from __future__ import annotations

from datetime import datetime

from errors import DomainError

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _minutes(value: str) -> int:
    parsed = datetime.strptime(value, "%H:%M")
    return parsed.hour * 60 + parsed.minute


def _hm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _overlap(a_start: str, a_duration: int, b_start: str, b_duration: int) -> bool:
    start_a, start_b = _minutes(a_start), _minutes(b_start)
    return start_a < start_b + b_duration and start_b < start_a + a_duration


class Scheduler:
    """Adjudicates slots against immutable program versions.

    All checks read the version row handed in, never the program's current
    version, so historical and pinned slots are judged by the material known
    when they were arranged.
    """

    def __init__(self, db) -> None:
        self.db = db

    @property
    def conn(self):
        return self.db.conn

    # ---- validation -------------------------------------------------------
    def get_version(self, version_id: int):
        version = self.conn.execute(
            "SELECT pv.*, p.title, p.kind, p.active FROM program_versions pv "
            "JOIN programs p ON p.id=pv.program_id WHERE pv.id=?", (version_id,)
        ).fetchone()
        if not version:
            raise DomainError("节目版本不存在")
        return version

    def current_version_id(self, program_id: int) -> int:
        row = self.conn.execute(
            "SELECT current_version_id FROM programs WHERE id=?", (program_id,)
        ).fetchone()
        if not row or not row["current_version_id"]:
            raise DomainError("节目不存在")
        return int(row["current_version_id"])

    def validate(self, air_date: str, start_time: str, duration: int, version_id: int,
                 region: str, ignore_slot_id: int | None = None) -> None:
        try:
            day = datetime.strptime(air_date, "%Y-%m-%d").date()
        except ValueError as exc:
            raise DomainError("播出日期必须使用 YYYY-MM-DD") from exc
        try:
            start_minutes = _minutes(start_time)
        except ValueError as exc:
            raise DomainError("开始时间必须使用 HH:MM") from exc
        if duration <= 0:
            raise DomainError("排期时长必须大于0")
        program = self.get_version(version_id)
        if not program["active"]:
            raise DomainError("节目未启用")
        if program["duration_minutes"] != duration:
            raise DomainError(f"排期时长必须等于节目时长 {program['duration_minutes']} 分钟")
        if start_minutes + duration > 24 * 60:
            raise DomainError("节目在当日内无法播完")
        if not (program["start_date"] <= air_date <= program["end_date"]):
            raise DomainError("播出日期超出授权窗口")
        if not self.conn.execute(
            "SELECT 1 FROM program_regions WHERE program_version_id=? AND region=?", (version_id, region)
        ).fetchone():
            raise DomainError(f"节目未授权在{region}播出")
        end_minutes = start_minutes + duration
        for window in self.conn.execute(
            "SELECT * FROM blocked_windows WHERE region=? AND weekday=?", (region, day.weekday())
        ).fetchall():
            if _minutes(window["start_time"]) < end_minutes and start_minutes < _minutes(window["end_time"]):
                raise DomainError(f"与禁播时段冲突: {window['reason']}")
        sql = "SELECT * FROM slots WHERE air_date=? AND region=? AND status!='cancelled'"
        params: list[object] = [air_date, region]
        if ignore_slot_id is not None:
            sql += " AND id!=?"
            params.append(ignore_slot_id)
        for existing in self.conn.execute(sql, params).fetchall():
            if _overlap(start_time, duration, existing["start_time"], existing["duration_minutes"]):
                raise DomainError(f"与排期 #{existing['id']} 时间重叠")
        if program["cooldown_minutes"]:
            previous = self.conn.execute(
                "SELECT s.* FROM slots s JOIN program_versions pv ON pv.id=s.program_version_id "
                "WHERE s.air_date=? AND s.region=? AND pv.program_id=? AND s.status!='cancelled' "
                "AND s.id!=? AND s.start_time < ? ORDER BY s.start_time DESC LIMIT 1",
                (air_date, region, program["program_id"], ignore_slot_id or -1, start_time),
            ).fetchone()
            if previous:
                gap = start_minutes - (_minutes(previous["start_time"]) + previous["duration_minutes"])
                if gap < program["cooldown_minutes"]:
                    raise DomainError(f"与上一期节目间隔不足冷却时间 {program['cooldown_minutes']} 分钟")
        if program["sponsor"]:
            policy = self.conn.execute(
                "SELECT min_gap_minutes FROM sponsor_policies WHERE sponsor=?", (program["sponsor"],)
            ).fetchone()
            if policy:
                gap = policy["min_gap_minutes"]
                all_sponsored = self.conn.execute(
                    "SELECT s.* FROM slots s JOIN program_versions pv ON pv.id=s.program_version_id "
                    "WHERE s.air_date=? AND s.region=? AND s.status!='cancelled' AND pv.sponsor=? AND s.id!=?",
                    (air_date, region, program["sponsor"], ignore_slot_id or -1),
                ).fetchall()
                for other in all_sponsored:
                    if _overlap(start_time, duration, other["start_time"], other["duration_minutes"]):
                        raise DomainError(f"与赞助商 {program['sponsor']} 的其他节目冲突")
                    distance = abs(start_minutes - (_minutes(other["start_time"]) + other["duration_minutes"]))
                    if distance < gap:
                        raise DomainError(f"与赞助商 {program['sponsor']} 的节目间隔不足 {gap} 分钟")

    def earliest_playable_start(self, air_date: str, start_time: str, version_id: int,
                                region: str, ignore_slot_id: int | None = None) -> str | None:
        """First start at or after the original time for which the version passes.

        Static disqualifications (region not licensed at all, date outside the
        license window) mean nothing on that day can work, so return None.
        Otherwise scan the rest of the day minute by minute.
        """
        version = self.get_version(version_id)
        if not self.conn.execute(
            "SELECT 1 FROM program_regions WHERE program_version_id=? AND region=?", (version_id, region)
        ).fetchone():
            return None
        if not (version["start_date"] <= air_date <= version["end_date"]):
            return None
        duration = int(version["duration_minutes"])
        try:
            cursor = _minutes(start_time)
        except ValueError:
            cursor = 0
        while cursor + duration <= 24 * 60:
            probe = _hm(cursor)
            try:
                self.validate(air_date, probe, duration, version_id, region, ignore_slot_id)
                return probe
            except DomainError:
                cursor += 1
        return None

    # ---- mutation entry points -------------------------------------------
    def schedule_slot(self, air_date: str, start_time: str, program_id: int, region: str) -> int:
        version_id = self.current_version_id(program_id)
        version = self.get_version(version_id)
        with self.db.transaction():
            self.validate(air_date, start_time, int(version["duration_minutes"]), version_id, region)
            cur = self.conn.execute(
                "INSERT INTO slots(air_date,start_time,duration_minutes,program_version_id,region,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (air_date, start_time, int(version["duration_minutes"]), version_id, region,
                 datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def replace_slot(self, slot_id: int, new_program_id: int) -> dict:
        """Replace a planned item and revalidate the resulting plan atomically."""
        new_version_id = self.current_version_id(new_program_id)
        with self.db.transaction():
            slot = self.conn.execute("SELECT * FROM slots WHERE id=? AND status='planned'", (slot_id,)).fetchone()
            if not slot:
                raise DomainError("只能替换尚未播出且状态为 planned 的排期")
            program = self.get_version(new_version_id)
            self.validate(slot["air_date"], slot["start_time"], int(program["duration_minutes"]),
                          new_version_id, slot["region"], slot_id)
            self.conn.execute(
                "UPDATE slots SET program_version_id=?, duration_minutes=?, replaced_from_version_id=?, "
                "status='replaced', revision_reason=NULL, revision_air_date=NULL, revision_start_time=NULL, "
                "revision_duration_minutes=NULL, revision_candidate_version_id=NULL, revision_earliest_start=NULL "
                "WHERE id=?",
                (new_version_id, int(program["duration_minutes"]), slot["program_version_id"], slot_id),
            )
        return self.get_slot(slot_id)

    def revise_slot(self, slot_id: int, start_time: str | None = None, region: str | None = None) -> dict:
        """Operator follow-up for a returned slot: recheck against the candidate.

        Only ``needs_revision`` slots can be revised. A pass adopts the new
        version at the (possibly edited) time and region; a failure keeps the
        slot returned with the refreshed reason and earliest playable start.
        """
        with self.db.transaction():
            slot = self.conn.execute("SELECT * FROM slots WHERE id=?", (slot_id,)).fetchone()
            if not slot:
                raise DomainError("排期不存在")
            if slot["status"] != "needs_revision":
                raise DomainError("只有待改(needs_revision)排期可以修改重查")
            candidate_id = slot["revision_candidate_version_id"]
            candidate = self.get_version(candidate_id)
            target_start = start_time or slot["revision_start_time"]
            target_region = region or slot["region"]
            try:
                self.validate(slot["revision_air_date"], target_start, int(candidate["duration_minutes"]),
                              candidate_id, target_region, slot_id)
            except DomainError as exc:
                earliest = self.earliest_playable_start(
                    slot["revision_air_date"], target_start, candidate_id, target_region, slot_id
                )
                self.conn.execute(
                    "UPDATE slots SET start_time=?, region=?, revision_reason=?, revision_earliest_start=? "
                    "WHERE id=?",
                    (target_start, target_region, str(exc), earliest, slot_id),
                )
                raise
            self.conn.execute(
                "UPDATE slots SET start_time=?, region=?, program_version_id=?, duration_minutes=?, "
                "status='planned', revision_reason=NULL, revision_air_date=NULL, revision_start_time=NULL, "
                "revision_duration_minutes=NULL, revision_candidate_version_id=NULL, revision_earliest_start=NULL "
                "WHERE id=?",
                (target_start, target_region, candidate_id, int(candidate["duration_minutes"]), slot_id),
            )
        return self.get_slot(slot_id)

    def recheck_for_version(self, program_id: int, new_version_id: int) -> dict:
        """Re-adjudicate every live slot of a program after a new version ships.

        Open slots (planned or returned, no playout) are retried on the new
        version: pass -> reference swapped; fail -> returned for revision with
        the original time, reason and earliest playable start. Replaced slots
        and slots with playout history stay pinned to their historical version.
        Must run inside the caller's transaction.
        """
        candidates = self.conn.execute(
            "SELECT s.* FROM slots s JOIN program_versions pv ON pv.id=s.program_version_id "
            "WHERE pv.program_id=? AND s.status IN ('planned','needs_revision') "
            "AND NOT EXISTS (SELECT 1 FROM playout_logs l WHERE l.slot_id=s.id) "
            "ORDER BY s.air_date,s.start_time,s.id",
            (program_id,),
        ).fetchall()
        upgraded: list[int] = []
        needs_revision: list[dict] = []
        for slot in candidates:
            version = self.get_version(new_version_id)
            target_start = slot["start_time"]
            try:
                self.validate(slot["air_date"], target_start, int(version["duration_minutes"]),
                              new_version_id, slot["region"], slot["id"])
            except DomainError as exc:
                earliest = self.earliest_playable_start(
                    slot["air_date"], target_start, new_version_id, slot["region"], slot["id"]
                )
                self.conn.execute(
                    "UPDATE slots SET status='needs_revision', revision_air_date=air_date, "
                    "revision_start_time=start_time, revision_duration_minutes=duration_minutes, "
                    "revision_reason=?, revision_candidate_version_id=?, revision_earliest_start=? WHERE id=?",
                    (str(exc), new_version_id, earliest, slot["id"]),
                )
                needs_revision.append({"slot_id": slot["id"], "air_date": slot["air_date"],
                                       "start_time": slot["start_time"],
                                       "original_duration_minutes": slot["duration_minutes"],
                                       "reason": str(exc), "earliest_start": earliest})
            else:
                self.conn.execute(
                    "UPDATE slots SET program_version_id=?, duration_minutes=?, status='planned', "
                    "revision_reason=NULL, revision_air_date=NULL, revision_start_time=NULL, "
                    "revision_duration_minutes=NULL, revision_candidate_version_id=NULL, "
                    "revision_earliest_start=NULL WHERE id=?",
                    (new_version_id, int(version["duration_minutes"]), slot["id"]),
                )
                upgraded.append(slot["id"])
        locked = [dict(row) for row in self.conn.execute(
            "SELECT s.id,s.status FROM slots s JOIN program_versions pv ON pv.id=s.program_version_id "
            "WHERE pv.program_id=? AND s.id NOT IN (%s)" %
            (",".join(str(c["id"]) for c in candidates) or "0"),
            (program_id,),
        ).fetchall()]
        return {"new_version_id": new_version_id, "upgraded": upgraded,
                "needs_revision": needs_revision, "locked": locked}

    def get_slot(self, slot_id: int) -> dict:
        row = self.conn.execute(
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
            WHERE s.id=?
            """,
            (slot_id,),
        ).fetchone()
        if not row:
            raise DomainError("排期不存在")
        return dict(row)
