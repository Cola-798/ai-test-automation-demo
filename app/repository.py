from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .schemas import ExecutionPlan, PlanRecord, SourceCase, ValidationResult


class CaseStateRepository:
    """Current case ownership and qualification state; replay stays elsewhere."""

    def __init__(self, database_path: Path, runs_dir: Path):
        self.database_path = database_path
        self.runs_dir = runs_dir
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS case_device_state (
                    case_id TEXT PRIMARY KEY,
                    owner_device_udid TEXT,
                    owner_device_name TEXT,
                    qualification_status TEXT NOT NULL DEFAULT 'NOT_RUN',
                    qualification_run_id TEXT,
                    qualification_plan_record_id INTEGER,
                    updated_at TEXT NOT NULL
                )
                """
            )
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(case_device_state)")
            }
            if "qualification_plan_record_id" not in columns:
                connection.execute(
                    "ALTER TABLE case_device_state ADD COLUMN qualification_plan_record_id INTEGER"
                )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS case_execution_scope (
                    case_id TEXT PRIMARY KEY,
                    owner_device_udid TEXT,
                    owner_device_name TEXT,
                    block_reason TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                )"""
            )

    def set_scope(
        self, case_id: str, *, owner_device_udid: str | None = None,
        owner_device_name: str | None = None, block_reason: str = "",
    ) -> None:
        if not case_id or (owner_device_udid and block_reason):
            raise ValueError("用例范围必须有编号，且阻塞用例不能同时分配设备")
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO case_execution_scope VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(case_id) DO UPDATE SET
                    owner_device_udid=excluded.owner_device_udid,
                    owner_device_name=excluded.owner_device_name,
                    block_reason=excluded.block_reason,
                    updated_at=excluded.updated_at""",
                (case_id, owner_device_udid, owner_device_name, block_reason,
                 datetime.now(timezone.utc).isoformat()),
            )

    def sync(
        self,
        records: list[PlanRecord],
        migrated_case_ids: set[str],
        primary_device_udid: str,
        fallback_device_udid: str,
        source_cases: dict[str, SourceCase] | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        case_ids = {
            record.plan.source.case_id
            for record in records
            if not record.plan.source.case_id.startswith("CLEANUP-")
        }
        case_ids.update(source_cases or {})
        current_record_ids: dict[str, int] = {}
        current_record_scores: dict[str, tuple[bool, int, int]] = {}
        for record in records:
            case_id = record.plan.source.case_id
            if case_id.startswith("CLEANUP-"):
                continue
            status = getattr(getattr(record.plan, "status", None), "value", getattr(record.plan, "status", "validated"))
            if status not in {"confirmed", "validated"}:
                continue
            score = (status == "validated", record.plan.version, record.record_id)
            if case_id not in current_record_scores or score > current_record_scores[case_id]:
                current_record_scores[case_id] = score
                current_record_ids[case_id] = record.record_id
        try:
            with self._connect() as connection:
                replay_run_ids = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT run_id FROM stability_attempts WHERE run_id IS NOT NULL"
                    ).fetchall()
                }
        except sqlite3.OperationalError:
            replay_run_ids = set()
        run_rows: dict[tuple[str, str], dict] = {}
        device_rows: dict[str, set[str]] = {}
        record_ids_by_plan = {
            (record.plan.plan_id, record.plan.version): record.record_id
            for record in records
            if getattr(record, "record_id", None) is not None
        }
        candidate_record_ids = {
            record.record_id for record in records
            if getattr(getattr(record.plan, "status", None), "value", None) == "candidate"
        }
        for result_path in self.runs_dir.glob("*/result.json"):
            if result_path.parent.name.startswith("evidence-"):
                continue
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            case_id = str(result.get("case_id") or "")
            udid = str((result.get("device") or {}).get("udid") or "")
            if case_id not in case_ids or not udid:
                continue
            plan_record_id = result.get("plan_record_id")
            if plan_record_id is None:
                try:
                    plan_payload = json.loads((result_path.parent / "plan.json").read_text(encoding="utf-8"))
                    plan_record_id = record_ids_by_plan.get(
                        (str(plan_payload.get("plan_id") or ""), int(plan_payload.get("version") or 1))
                    )
                except (OSError, json.JSONDecodeError, TypeError, ValueError):
                    plan_record_id = None
            result = {**result, "qualification_plan_record_id": plan_record_id}
            if plan_record_id in candidate_record_ids:
                continue
            device_rows.setdefault(case_id, set()).add(udid)
            if result_path.parent.name in replay_run_ids:
                continue
            key = (case_id, udid)
            current = run_rows.get(key)
            if current is None or str(result.get("finished_at") or "") > str(current.get("finished_at") or ""):
                run_rows[key] = {**result, "run_id": result_path.parent.name}

        # The imported first-run ledger is the qualification source of truth;
        # stability batches may reuse the same plan but must not replace it.
        artifact_dir = self.runs_dir.parent / "artifacts" / "first_run"
        for result_path in artifact_dir.glob("*/result.json"):
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            case_id = str(result.get("case_id") or "")
            udid = str((result.get("device") or {}).get("udid") or "")
            if case_id not in case_ids or not udid:
                continue
            plan_record_id = result.get("plan_record_id")
            if plan_record_id is None:
                try:
                    plan_payload = json.loads((result_path.parent / "plan.json").read_text(encoding="utf-8"))
                    plan_record_id = record_ids_by_plan.get(
                        (str(plan_payload.get("plan_id") or ""), int(plan_payload.get("version") or 1))
                    )
                except (OSError, json.JSONDecodeError, TypeError, ValueError):
                    plan_record_id = None
            result = {**result, "qualification_plan_record_id": plan_record_id}
            if plan_record_id in candidate_record_ids:
                continue
            device_rows.setdefault(case_id, set()).add(udid)
            key = (case_id, udid)
            current = run_rows.get(key)
            if current is None or str(result.get("finished_at") or "") > str(current.get("finished_at") or ""):
                run_rows[key] = {**result, "run_id": f"first-run:{result_path.parent.name}"}

        with self._connect() as connection:
            scope_rows = {
                row["case_id"]: row
                for row in connection.execute("SELECT * FROM case_execution_scope")
            }
            for case_id in sorted(case_ids):
                devices = device_rows.get(case_id, set())
                scope = scope_rows.get(case_id)
                if scope and scope["block_reason"]:
                    owner = None
                elif scope and scope["owner_device_udid"]:
                    owner = scope["owner_device_udid"]
                elif case_id in migrated_case_ids:
                    owner = primary_device_udid
                elif len(devices) == 1:
                    owner = next(iter(devices))
                elif len(devices) > 1:
                    owner = fallback_device_udid if fallback_device_udid in devices else sorted(devices)[0]
                else:
                    owner = None
                latest = run_rows.get((case_id, owner)) if owner else None
                status = "NOT_RUN"
                run_id = None
                if latest:
                    run_id = latest["run_id"]
                    if latest.get("status") == "passed":
                        status = "PASSED"
                    elif latest.get("classification") in {"ENVIRONMENT_EXCEPTION", "TEST_DATA_FAILURE"}:
                        status = "BLOCKED"
                    else:
                        status = "FAILED"
                    source_case = (source_cases or {}).get(case_id)
                    qualification_matches_current = (
                        latest.get("qualification_plan_record_id")
                        == current_record_ids.get(case_id)
                        and source_case is not None
                        and any(
                            record.record_id == current_record_ids.get(case_id)
                            and record.plan.source == source_case
                            for record in records
                        )
                    )
                    if status == "PASSED" and not qualification_matches_current:
                        status = "NOT_RUN"
                        run_id = None
                if scope and scope["block_reason"]:
                    status, run_id = "BLOCKED", None
                owner_name = (
                    scope["owner_device_name"] if scope and scope["owner_device_name"]
                    else (latest or {}).get("device", {}).get("name") if latest else None
                )
                connection.execute(
                    """
                    INSERT INTO case_device_state
                        (case_id, owner_device_udid, owner_device_name, qualification_status, qualification_run_id,
                         qualification_plan_record_id, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(case_id) DO UPDATE SET
                        owner_device_udid=excluded.owner_device_udid,
                        owner_device_name=COALESCE(excluded.owner_device_name, case_device_state.owner_device_name),
                        qualification_status=excluded.qualification_status,
                        qualification_run_id=excluded.qualification_run_id,
                        qualification_plan_record_id=excluded.qualification_plan_record_id,
                        updated_at=excluded.updated_at
                    """,
                    (case_id, owner, owner_name, status, run_id,
                     latest.get("qualification_plan_record_id") if latest else None, now),
                )

    def list(self, device_udid: str | None = None) -> list[dict]:
        query = (
            "SELECT case_device_state.*, COALESCE(case_execution_scope.block_reason, '') AS block_reason "
            "FROM case_device_state LEFT JOIN case_execution_scope USING(case_id)"
        )
        args: tuple[str, ...] = ()
        if device_udid:
            query += " WHERE case_device_state.owner_device_udid = ?"
            args = (device_udid,)
        query += " ORDER BY case_device_state.case_id"
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(query, args).fetchall()]


class SourceCaseRepository:
    """Durable imported-case directory with immutable source revisions."""

    def __init__(self, database_path: Path):
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS source_cases (
                    case_id TEXT PRIMARY KEY,
                    current_revision INTEGER NOT NULL,
                    source_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS source_case_revisions (
                    case_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    source_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(case_id, revision)
                )"""
            )

    def import_cases(self, cases: list[SourceCase]) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        incoming: dict[str, SourceCase] = {}
        for case in cases:
            existing = incoming.get(case.case_id)
            if existing is not None and existing != case:
                raise ValueError(f"同一文件中用例 {case.case_id} 内容冲突，未导入")
            incoming[case.case_id] = case
        inserted = revised = unchanged = 0
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            for case_id, case in incoming.items():
                serialized = case.model_dump_json()
                row = connection.execute(
                    "SELECT current_revision, source_json, created_at FROM source_cases WHERE case_id=?",
                    (case_id,),
                ).fetchone()
                if row is None:
                    revision = 1
                    connection.execute(
                        "INSERT INTO source_cases VALUES (?, ?, ?, ?, ?)",
                        (case_id, revision, serialized, now, now),
                    )
                    connection.execute(
                        "INSERT INTO source_case_revisions VALUES (?, ?, ?, ?)",
                        (case_id, revision, serialized, now),
                    )
                    inserted += 1
                elif SourceCase.model_validate_json(row[1]) == case:
                    unchanged += 1
                else:
                    revision = int(row[0]) + 1
                    connection.execute(
                        "UPDATE source_cases SET current_revision=?, source_json=?, updated_at=? WHERE case_id=?",
                        (revision, serialized, now, case_id),
                    )
                    connection.execute(
                        "INSERT INTO source_case_revisions VALUES (?, ?, ?, ?)",
                        (case_id, revision, serialized, now),
                    )
                    revised += 1
        return {
            "inserted": inserted,
            "revised": revised,
            "unchanged": unchanged,
            "total": len(self.list()),
        }

    def sync_from_plans(self, records: list[PlanRecord]) -> int:
        """Bootstrap the durable directory from existing saved plan sources once."""
        candidates: dict[str, PlanRecord] = {}
        for record in records:
            case = record.plan.source
            if case.case_id.startswith("CLEANUP-"):
                continue
            current = candidates.get(case.case_id)
            if current is None or (record.plan.version, record.updated_at) > (
                current.plan.version, current.updated_at
            ):
                candidates[case.case_id] = record
        existing = {item.case_id for item in self.list()}
        return self.import_cases(
            [record.plan.source for case_id, record in candidates.items() if case_id not in existing]
        )["inserted"]

    def ensure_source(self, case: SourceCase) -> None:
        now = datetime.now(timezone.utc).isoformat()
        serialized = case.model_dump_json()
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT 1 FROM source_cases WHERE case_id=?", (case.case_id,)
            ).fetchone()
            if existing:
                return
            connection.execute(
                "INSERT INTO source_cases VALUES (?, 1, ?, ?, ?)",
                (case.case_id, serialized, now, now),
            )
            connection.execute(
                "INSERT INTO source_case_revisions VALUES (?, 1, ?, ?)",
                (case.case_id, serialized, now),
            )

    def list(self) -> list[SourceCase]:
        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT source_json FROM source_cases ORDER BY case_id"
            ).fetchall()
        return [SourceCase.model_validate_json(row[0]) for row in rows]

    def revisions(self, case_id: str) -> list[dict]:
        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT revision, source_json, created_at FROM source_case_revisions "
                "WHERE case_id=? ORDER BY revision",
                (case_id,),
            ).fetchall()
        return [
            {
                "revision": row[0],
                "source": SourceCase.model_validate_json(row[1]).model_dump(mode="json"),
                "created_at": row[2],
            }
            for row in rows
        ]


class PlanRepository:
    def __init__(self, database_path: Path):
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS plans (
                    record_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    validation_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(plan_id, version)
                )
                """
            )

    def save(self, plan: ExecutionPlan, validation: ValidationResult) -> PlanRecord:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO plans(plan_id, version, status, plan_json, validation_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan.plan_id,
                    plan.version,
                    plan.status,
                    plan.model_dump_json(),
                    validation.model_dump_json(),
                    now,
                    now,
                ),
            )
            record_id = int(cursor.lastrowid)
        return PlanRecord(record_id=record_id, plan=plan, validation=validation, created_at=now, updated_at=now)

    def list(self) -> list[PlanRecord]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM plans ORDER BY record_id DESC").fetchall()
        return [self._to_record(row) for row in rows]

    def get(self, record_id: int) -> PlanRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM plans WHERE record_id = ?", (record_id,)).fetchone()
        return self._to_record(row) if row else None

    def update(self, record_id: int, plan: ExecutionPlan, validation: ValidationResult) -> PlanRecord:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            current = connection.execute(
                "SELECT created_at FROM plans WHERE record_id = ?", (record_id,)
            ).fetchone()
            if current is None:
                raise KeyError(record_id)
            connection.execute(
                """
                UPDATE plans
                SET status = ?, plan_json = ?, validation_json = ?, updated_at = ?
                WHERE record_id = ?
                """,
                (
                    plan.status,
                    plan.model_dump_json(),
                    validation.model_dump_json(),
                    now,
                    record_id,
                ),
            )
        return PlanRecord(
            record_id=record_id,
            plan=plan,
            validation=validation,
            created_at=current["created_at"],
            updated_at=now,
        )

    @staticmethod
    def _to_record(row: sqlite3.Row) -> PlanRecord:
        return PlanRecord(
            record_id=row["record_id"],
            plan=ExecutionPlan.model_validate(json.loads(row["plan_json"])),
            validation=ValidationResult.model_validate(json.loads(row["validation_json"])),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
