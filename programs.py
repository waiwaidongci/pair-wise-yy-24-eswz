"""节目维护：节目资料的每次修改都生成新版本，旧版本停用、仅保留作历史。"""

from __future__ import annotations

from datetime import datetime

from database import Database, DomainError

PROGRAM_KINDS = {"music", "ad", "talk", "live"}

#: update_program 的 sponsor 未传时的哨兵；显式传 None 表示清除赞助商
UNSET = object()


class ProgramService:
    def __init__(self, store: Database) -> None:
        self.store = store

    def _validate(self, title: str, kind: str, duration_minutes: int, start_date: str, end_date: str,
                  cooldown_minutes: int) -> None:
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

    def get(self, program_id: int) -> dict:
        row = self.store.conn.execute("SELECT * FROM programs WHERE id=?", (program_id,)).fetchone()
        if not row:
            raise DomainError("节目不存在")
        return dict(row)

    def regions_of(self, program_id: int) -> list[str]:
        return [row["region"] for row in self.store.conn.execute(
            "SELECT region FROM program_regions WHERE program_id=? ORDER BY region", (program_id,)
        ).fetchall()]

    @staticmethod
    def _clean_regions(regions) -> list[str]:
        cleaned: list[str] = []
        for region in regions or []:
            name = str(region).strip()
            if name and name not in cleaned:
                cleaned.append(name)
        return cleaned

    def _insert_version(self, root_id: int | None, version: int, title: str, kind: str, duration_minutes: int,
                        start_date: str, end_date: str, sponsor: str | None, cooldown_minutes: int,
                        regions: list[str]) -> int:
        cur = self.store.conn.execute(
            "INSERT INTO programs(root_id,version,title,kind,duration_minutes,start_date,end_date,sponsor,"
            "cooldown_minutes,active,created_at) VALUES(?,?,?,?,?,?,?,?,?,1,?)",
            (root_id, version, title.strip(), kind, duration_minutes, start_date, end_date,
             (sponsor or "").strip() or None, cooldown_minutes, datetime.now().isoformat()),
        )
        program_id = int(cur.lastrowid)
        if root_id is None:
            self.store.conn.execute("UPDATE programs SET root_id=? WHERE id=?", (program_id, program_id))
        for region in regions:
            self.store.conn.execute(
                "INSERT INTO program_regions(program_id,region) VALUES(?,?)", (program_id, region)
            )
        return program_id

    def add_program(self, title: str, kind: str, duration_minutes: int, start_date: str, end_date: str,
                    sponsor: str | None = None, cooldown_minutes: int = 0,
                    regions: list[str] | None = None) -> int:
        self._validate(title, kind, duration_minutes, start_date, end_date, cooldown_minutes)
        with self.store.transaction():
            return self._insert_version(None, 1, title, kind, duration_minutes, start_date, end_date,
                                        sponsor, cooldown_minutes, self._clean_regions(regions))

    def update_program(self, program_id: int, *, title: str | None = None, kind: str | None = None,
                       duration_minutes: int | None = None, start_date: str | None = None,
                       end_date: str | None = None, sponsor=UNSET, cooldown_minutes: int | None = None,
                       regions: list[str] | None = None) -> dict:
        """基于当前版本生成新版本；旧版本立即停用，仅保留作历史。"""
        with self.store.transaction():
            old = self.store.conn.execute("SELECT * FROM programs WHERE id=?", (program_id,)).fetchone()
            if not old:
                raise DomainError("节目不存在")
            if not old["active"]:
                raise DomainError("历史版本只读，请对当前版本做修改")
            merged = {
                "title": old["title"] if title is None else str(title),
                "kind": old["kind"] if kind is None else str(kind),
                "duration_minutes": old["duration_minutes"] if duration_minutes is None else int(duration_minutes),
                "start_date": old["start_date"] if start_date is None else str(start_date),
                "end_date": old["end_date"] if end_date is None else str(end_date),
                "sponsor": old["sponsor"] if sponsor is UNSET else sponsor,
                "cooldown_minutes": old["cooldown_minutes"] if cooldown_minutes is None else int(cooldown_minutes),
            }
            self._validate(merged["title"], merged["kind"], merged["duration_minutes"],
                           merged["start_date"], merged["end_date"], merged["cooldown_minutes"])
            new_regions = self.regions_of(program_id) if regions is None else self._clean_regions(regions)
            self.store.conn.execute("UPDATE programs SET active=0 WHERE id=?", (program_id,))
            new_id = self._insert_version(
                old["root_id"] or old["id"], old["version"] + 1, merged["title"], merged["kind"],
                merged["duration_minutes"], merged["start_date"], merged["end_date"], merged["sponsor"],
                merged["cooldown_minutes"], new_regions,
            )
        result = self.get(new_id)
        result["regions"] = new_regions
        result["supersedes"] = program_id
        return result

    def authorize_region(self, program_id: int, region: str) -> dict:
        """追加地区授权：同样视为一次修改，生成新版本。"""
        name = str(region).strip()
        if not name:
            raise DomainError("地区不能为空")
        with self.store.transaction():
            current = self.get(program_id)
            if not current["active"]:
                raise DomainError("历史版本只读，请对当前版本做修改")
            regions = self.regions_of(program_id)
            if name in regions:
                current["regions"] = regions
                current["supersedes"] = program_id
                return current
            regions.append(name)
            return self.update_program(program_id, regions=regions)
