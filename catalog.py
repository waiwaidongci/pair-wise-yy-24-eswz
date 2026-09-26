from __future__ import annotations

from datetime import datetime

from errors import DomainError, KEEP

PROGRAM_KINDS = {"music", "ad", "talk", "live"}


class ProgramCatalog:
    """Program maintenance: identities are stable, every edit is a new version.

    Versions are immutable once created. Slots pin a specific version, so old
    versions remain for history while open slots are re-adjudicated against
    the freshly created version.
    """

    def __init__(self, db) -> None:
        self.db = db

    @property
    def conn(self):
        return self.db.conn

    def _check_fields(self, title: str, kind: str, duration_minutes: int,
                      start_date: str, end_date: str, cooldown_minutes: int) -> None:
        if not title.strip():
            raise DomainError("节目名称不能为空")
        if kind not in PROGRAM_KINDS:
            raise DomainError(f"不支持的节目类型: {kind}")
        if duration_minutes <= 0:
            raise DomainError("节目时长必须大于0")
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d").date()
            end = datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError as exc:
            raise DomainError("日期必须使用 YYYY-MM-DD") from exc
        if end < start:
            raise DomainError("授权结束日期不能早于开始日期")
        if cooldown_minutes < 0:
            raise DomainError("冷却时间不能为负数")

    def add_program(self, title: str, kind: str, duration_minutes: int, start_date: str, end_date: str,
                    sponsor: str | None = None, cooldown_minutes: int = 0,
                    regions: list[str] | None = None) -> int:
        self._check_fields(title, kind, duration_minutes, start_date, end_date, cooldown_minutes)
        clean_regions = [r.strip() for r in (regions or []) if r.strip()]
        with self.db.transaction():
            existing = self.conn.execute("SELECT 1 FROM programs WHERE title=?", (title.strip(),)).fetchone()
            if existing:
                raise DomainError(f"节目《{title.strip()}》已存在")
            cur = self.conn.execute(
                "INSERT INTO programs(title,kind,active) VALUES(?,?,1)", (title.strip(), kind)
            )
            program_id = int(cur.lastrowid)
            version_id = self._insert_version(
                program_id, 1, duration_minutes, start_date, end_date, sponsor, cooldown_minutes, clean_regions
            )
            self.conn.execute("UPDATE programs SET current_version_id=? WHERE id=?", (version_id, program_id))
        return program_id

    def _insert_version(self, program_id: int, version_no: int, duration_minutes: int,
                        start_date: str, end_date: str, sponsor: str | None,
                        cooldown_minutes: int, regions: list[str]) -> int:
        cur = self.conn.execute(
            "INSERT INTO program_versions(program_id,version_no,duration_minutes,start_date,end_date,"
            "sponsor,cooldown_minutes,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (program_id, version_no, duration_minutes, start_date, end_date,
             (sponsor or "").strip() or None, cooldown_minutes, datetime.now().isoformat()),
        )
        version_id = int(cur.lastrowid)
        for region in regions:
            self.conn.execute(
                "INSERT INTO program_regions(program_version_id,region) VALUES(?,?)", (version_id, region)
            )
        return version_id

    def revise_program(self, program_id: int, *, duration_minutes=KEEP, start_date=KEEP, end_date=KEEP,
                       sponsor=KEEP, cooldown_minutes=KEEP, regions=KEEP) -> dict:
        """Create a new immutable version and re-adjudicate open slots.

        Fields that are omitted keep their previous values; pass an explicit
        value (including ``None`` for sponsor or ``[]`` for regions) to change.
        Returns the new version id together with the slot recheck report.
        """
        with self.db.transaction():
            current = self.conn.execute(
                "SELECT p.id,p.title,p.kind,p.current_version_id,pv.* FROM programs p "
                "JOIN program_versions pv ON pv.id=p.current_version_id WHERE p.id=?",
                (program_id,),
            ).fetchone()
            if not current:
                raise DomainError("节目不存在")
            new_values = {
                "duration_minutes": int(duration_minutes) if duration_minutes is not KEEP else current["duration_minutes"],
                "start_date": start_date if start_date is not KEEP else current["start_date"],
                "end_date": end_date if end_date is not KEEP else current["end_date"],
                "sponsor": sponsor if sponsor is not KEEP else current["sponsor"],
                "cooldown_minutes": int(cooldown_minutes) if cooldown_minutes is not KEEP else current["cooldown_minutes"],
            }
            if regions is KEEP:
                new_regions = [r["region"] for r in self.conn.execute(
                    "SELECT region FROM program_regions WHERE program_version_id=? ORDER BY region",
                    (current["current_version_id"],)
                ).fetchall()]
            else:
                new_regions = sorted({r.strip() for r in regions if r.strip()})
            self._check_fields(current["title"], current["kind"], new_values["duration_minutes"],
                               new_values["start_date"], new_values["end_date"], new_values["cooldown_minutes"])
            unchanged = (
                new_values["duration_minutes"] == current["duration_minutes"]
                and new_values["start_date"] == current["start_date"]
                and new_values["end_date"] == current["end_date"]
                and (new_values["sponsor"] or None) == (current["sponsor"] or None)
                and new_values["cooldown_minutes"] == current["cooldown_minutes"]
            )
            old_regions = [r["region"] for r in self.conn.execute(
                "SELECT region FROM program_regions WHERE program_version_id=? ORDER BY region",
                (current["current_version_id"],)
            ).fetchall()]
            if unchanged and new_regions == old_regions:
                raise DomainError("节目资料没有变化，无需生成新版本")
            new_version_no = int(current["version_no"]) + 1
            version_id = self._insert_version(
                program_id, new_version_no, new_values["duration_minutes"], new_values["start_date"],
                new_values["end_date"], new_values["sponsor"], new_values["cooldown_minutes"], new_regions
            )
            self.conn.execute("UPDATE programs SET current_version_id=? WHERE id=?", (version_id, program_id))
            report = self.db.scheduler.recheck_for_version(program_id, version_id)
            report["program_id"] = program_id
            report["version_no"] = new_version_no
        return report

    def authorize_region(self, program_id: int, region: str) -> dict:
        """Grant a region by issuing a new version (old versions keep history)."""
        if not region.strip():
            raise DomainError("地区不能为空")
        current = self.conn.execute(
            "SELECT pv.id FROM programs p JOIN program_versions pv ON pv.id=p.current_version_id WHERE p.id=?",
            (program_id,),
        ).fetchone()
        if not current:
            raise DomainError("节目不存在")
        regions = [r["region"] for r in self.conn.execute(
            "SELECT region FROM program_regions WHERE program_version_id=?", (current["id"],)
        ).fetchall()]
        region = region.strip()
        if region in regions:
            raise DomainError(f"节目已授权在{region}播出")
        regions.append(region)
        return self.revise_program(program_id, regions=sorted(regions))
