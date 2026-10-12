from __future__ import annotations

import json
import inspect
import sqlite3
import threading
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal
from uuid import uuid4

from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field, field_validator

from .repository import PlanRepository
from .repair import RepairPatchStore, verified_patches_from_result
from .schemas import DevicePlatform, ExecutionPlan, PlanStatus


AUTOMATION_COMPLETED = {
    "NORMAL_PASS",
    "AI_REPAIR_SUCCESS",
    "AI_VISUAL_ORACLE_PASS",
    "ENVIRONMENT_RECOVERED_PASS",
    "PRODUCT_FAILURE",
    "ACCEPTANCE_MISMATCH",
}
PRODUCT_PASS = {"NORMAL_PASS", "AI_REPAIR_SUCCESS", "AI_VISUAL_ORACLE_PASS", "ENVIRONMENT_RECOVERED_PASS"}
AUTOMATION_FAILURES = {"AUTOMATION_FAILURE", "AUTOMATION_FAILURE_REPAIRABLE"}
ENVIRONMENT_FAILURES = {"ENVIRONMENT_EXCEPTION"}
TEST_DATA_FAILURES = {"TEST_DATA_FAILURE"}


class StabilityBatchInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    plan_record_ids: list[int] = Field(min_length=1, max_length=100)
    repetitions: int = Field(ge=1, le=20)
    category: Literal["normal", "repair"] = "normal"
    platform: DevicePlatform = DevicePlatform.ANDROID
    device_udid: str | None = Field(default=None, max_length=200)

    @field_validator("plan_record_ids")
    @classmethod
    def unique_plan_ids(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)):
            raise ValueError("同一计划不能重复选择")
        return value


class EfficiencyBaselineInput(BaseModel):
    manual_execution_minutes_per_round: float = Field(ge=0, le=1440)
    manual_report_minutes_per_round: float = Field(ge=0, le=1440)
    automated_human_minutes_total: float = Field(ge=0, le=10000)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StabilityRepository:
    def __init__(self, database_path: Path):
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS stability_batches (
                    batch_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    category TEXT NOT NULL,
                    platform TEXT NOT NULL DEFAULT 'android',
                    repetitions INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    stop_requested INTEGER NOT NULL DEFAULT 0,
                    plan_record_ids_json TEXT NOT NULL,
                    manual_execution_minutes_per_round REAL,
                    manual_report_minutes_per_round REAL,
                    automated_human_minutes_total REAL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    error_message TEXT NOT NULL DEFAULT ''
                )
                """
            )
            self._ensure_column(
                connection,
                "stability_batches",
                "manual_execution_minutes_per_round",
                "REAL",
            )
            self._ensure_column(
                connection, "stability_batches", "platform", "TEXT NOT NULL DEFAULT 'android'"
            )
            self._ensure_column(connection, "stability_batches", "device_udid", "TEXT")
            self._ensure_column(connection, "stability_batches", "error_message", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(
                connection, "stability_batches", "manual_report_minutes_per_round", "REAL"
            )
            self._ensure_column(
                connection, "stability_batches", "automated_human_minutes_total", "REAL"
            )
            self._ensure_column(
                connection, "stability_batches", "plan_snapshots_json", "TEXT NOT NULL DEFAULT '{}'"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS stability_attempts (
                    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT NOT NULL,
                    sequence_no INTEGER NOT NULL,
                    plan_record_id INTEGER NOT NULL,
                    case_id TEXT NOT NULL,
                    case_title TEXT NOT NULL,
                    iteration INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    run_id TEXT,
                    classification TEXT,
                    message TEXT NOT NULL DEFAULT '',
                    started_at TEXT,
                    finished_at TEXT,
                    duration_seconds REAL,
                    locator_failures INTEGER NOT NULL DEFAULT 0,
                    ai_interventions INTEGER NOT NULL DEFAULT 0,
                    ai_repairs_succeeded INTEGER NOT NULL DEFAULT 0,
                    step_count INTEGER NOT NULL DEFAULT 0,
                    ai_step_count INTEGER NOT NULL DEFAULT 0,
                    environment_retry_history_json TEXT NOT NULL DEFAULT '[]',
                    UNIQUE(batch_id, plan_record_id, iteration),
                    FOREIGN KEY(batch_id) REFERENCES stability_batches(batch_id)
                )
                """
            )
            self._ensure_column(
                connection, "stability_attempts", "environment_retry_history_json", "TEXT NOT NULL DEFAULT '[]'"
            )

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        existing = {
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def recover_interrupted(self) -> None:
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """UPDATE stability_attempts
                   SET status = 'interrupted', classification = 'ENVIRONMENT_EXCEPTION',
                       message = '平台进程中断，结果不确定', finished_at = ?
                   WHERE status = 'running'""",
                (now,),
            )
            connection.execute(
                "UPDATE stability_attempts SET status = 'cancelled', message = '平台进程中断，未执行' WHERE status = 'queued' AND batch_id IN (SELECT batch_id FROM stability_batches WHERE status = 'running')"
            )
            connection.execute(
                "UPDATE stability_batches SET status = 'interrupted', finished_at = ? WHERE status = 'running'",
                (now,),
            )

    def create_batch(
        self,
        payload: StabilityBatchInput,
        plans: list,
        *,
        plan_snapshots: dict[int, dict] | None = None,
    ) -> dict:
        batch_id = f"batch-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6]}"
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO stability_batches
                   (batch_id, name, category, platform, device_udid, repetitions, status,
                    plan_record_ids_json, created_at, plan_snapshots_json)
                   VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?)""",
                (
                    batch_id,
                    payload.name,
                    payload.category,
                    payload.platform.value,
                    payload.device_udid,
                    payload.repetitions,
                    json.dumps(payload.plan_record_ids),
                    now,
                    json.dumps(plan_snapshots or {}, ensure_ascii=False),
                ),
            )
            sequence = 0
            for iteration in range(1, payload.repetitions + 1):
                for record in plans:
                    sequence += 1
                    connection.execute(
                        """INSERT INTO stability_attempts
                           (batch_id, sequence_no, plan_record_id, case_id, case_title, iteration, status)
                           VALUES (?, ?, ?, ?, ?, ?, 'queued')""",
                        (
                            batch_id,
                            sequence,
                            record.record_id,
                            record.plan.source.case_id,
                            record.plan.source.title,
                            iteration,
                        ),
                    )
        return self.get_batch(batch_id)

    def list_batches(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT batch_id FROM stability_batches ORDER BY created_at DESC"
            ).fetchall()
        return [self.get_batch(row["batch_id"], include_attempts=False) for row in rows]

    def latest_case_outcomes(
        self, device_udid: str | None = None, *, group_by_device: bool = False,
        exclude_record_ids: set[int] | None = None,
    ) -> dict:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT a.attempt_id, a.plan_record_id, a.case_id, a.case_title, a.status,
                       a.run_id, a.classification, a.finished_at,
                       b.batch_id, b.name AS batch_name, b.category, b.repetitions, b.created_at, b.device_udid
                FROM stability_attempts AS a
                JOIN stability_batches AS b ON b.batch_id = a.batch_id
                WHERE b.category != 'experiment'
                  AND EXISTS (
                    SELECT 1
                    FROM stability_attempts AS finished
                    WHERE finished.batch_id = a.batch_id
                      AND finished.plan_record_id = a.plan_record_id
                      AND finished.status = 'completed'
                )
                ORDER BY b.created_at DESC, b.batch_id DESC,
                         a.case_id, a.plan_record_id DESC, a.attempt_id ASC
                """
            ).fetchall()

        if exclude_record_ids:
            rows = [row for row in rows if row["plan_record_id"] not in exclude_record_ids]
        if device_udid:
            filtered = []
            for row in rows:
                if row["device_udid"] == device_udid:
                    filtered.append(row)
                    continue
                run_id = row["run_id"]
                if not run_id:
                    continue
                result_path = self.database_path.parent / "runs" / str(run_id) / "result.json"
                try:
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if str((result.get("device") or {}).get("udid") or "") == device_udid:
                    filtered.append(row)
            rows = filtered

        groups_by_case: dict[object, list[dict]] = {}
        groups_by_key: dict[tuple[str, str, int], dict] = {}
        for row in rows:
            case_id = row["case_id"]
            group_key = (case_id, row["batch_id"], row["plan_record_id"])
            group = groups_by_key.get(group_key)
            if group is None:
                group = {
                    "batch_id": row["batch_id"],
                    "batch_name": row["batch_name"],
                    "category": row["category"],
                    "plan_record_id": row["plan_record_id"],
                    "case_title": row["case_title"],
                    "repetitions": row["repetitions"],
                    "created_at": row["created_at"],
                    "attempts": [],
                }
                groups_by_key[group_key] = group
                outcome_key = (case_id, row["device_udid"]) if group_by_device else case_id
                groups_by_case.setdefault(outcome_key, []).append(group)
            group["attempts"].append(dict(row))

        outcomes: dict[object, dict] = {}
        for outcome_key, groups in groups_by_case.items():
            case_id = outcome_key[0] if group_by_device else outcome_key
            latest_group = groups[0]
            # Each batch is its own validation task. Earlier batches remain
            # available as history and must never contribute rounds here.
            target_group = latest_group
            attempts = target_group["attempts"]
            completed = [item for item in attempts if item["status"] == "completed"]
            completed_count = len(completed)
            required_attempts = int(target_group["repetitions"])
            replay_complete = completed_count >= required_attempts
            acceptance_passes = sum(item["classification"] in PRODUCT_PASS for item in completed)
            complete = replay_complete
            stable = complete and acceptance_passes == completed_count
            issue_categories: list[str] = []
            classifications = {item["classification"] for item in completed if item["classification"]}
            if classifications & {"PRODUCT_FAILURE", "ACCEPTANCE_MISMATCH"}:
                issue_categories.append("最终验收未通过")
            if classifications & AUTOMATION_FAILURES:
                issue_categories.append("轮次失败：自动化问题")
            if classifications & ENVIRONMENT_FAILURES:
                issue_categories.append("轮次失败：环境异常")
            if classifications & TEST_DATA_FAILURES:
                issue_categories.append("轮次失败：测试数据")
            if not replay_complete:
                issue_categories.append(f"未完成设定轮次（{completed_count}/{required_attempts}）")
            if complete and acceptance_passes < required_attempts and not issue_categories:
                issue_categories.append("轮次结果未达到验收通过")
            latest_run = max(
                (item for item in completed if item["run_id"]),
                key=lambda item: item["finished_at"] or "",
                default=None,
            )
            outcomes[outcome_key] = {
                "batch_id": latest_group["batch_id"],
                "batch_name": latest_group["batch_name"],
                "category": latest_group["category"],
                "plan_record_id": latest_group["plan_record_id"],
                "case_title": latest_group["case_title"],
                "repetitions": required_attempts,
                "completed_attempts": completed_count,
                "required_attempts": required_attempts,
                "normal_passes": sum(item["classification"] == "NORMAL_PASS" for item in completed),
                "acceptance_passes": acceptance_passes,
                "replay_complete": replay_complete,
                "complete": complete,
                "stable": stable,
                "issue_categories": issue_categories,
                "latest_run_id": latest_run["run_id"] if latest_run else None,
                "latest_finished_at": latest_run["finished_at"] if latest_run else None,
            }
        return outcomes

    def latest_unresolved_interruption_run_ids(self, device_udid: str | None = None) -> set[str]:
        """Keep a latest interrupted/failed stability run until a newer stable outcome replaces it."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT a.case_id, a.run_id, a.status, a.finished_at, a.attempt_id,
                       b.device_udid, b.created_at
                FROM stability_attempts AS a
                JOIN stability_batches AS b ON b.batch_id = a.batch_id
                WHERE b.category != 'experiment'
                  AND a.run_id IS NOT NULL AND a.status != 'completed'
                ORDER BY COALESCE(a.finished_at, b.created_at) DESC, a.attempt_id DESC
                """
            ).fetchall()
        latest: dict[tuple[str, str], sqlite3.Row] = {}
        for row in rows:
            row_device = str(row["device_udid"] or "")
            if device_udid and row_device != device_udid:
                continue
            latest.setdefault((str(row["case_id"]), row_device), row)
        outcomes = self.latest_case_outcomes(group_by_device=True)
        unresolved: set[str] = set()
        for key, row in latest.items():
            outcome = outcomes.get(key)
            interruption_time = str(row["finished_at"] or row["created_at"] or "")
            outcome_time = str((outcome or {}).get("latest_finished_at") or "")
            if outcome and outcome["stable"] and outcome_time >= interruption_time:
                continue
            unresolved.add(str(row["run_id"]))
        return unresolved

    def get_batch(self, batch_id: str, *, include_attempts: bool = True) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM stability_batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            if row is None:
                return None
            attempts = connection.execute(
                "SELECT * FROM stability_attempts WHERE batch_id = ? ORDER BY sequence_no",
                (batch_id,),
            ).fetchall()
        attempt_items = [dict(item) for item in attempts]
        for item in attempt_items:
            try:
                item["environment_retry_history"] = json.loads(item.pop("environment_retry_history_json") or "[]")
            except (json.JSONDecodeError, TypeError):
                item["environment_retry_history"] = []
        batch = dict(row)
        batch["stop_requested"] = bool(batch["stop_requested"])
        batch["plan_record_ids"] = json.loads(batch.pop("plan_record_ids_json"))
        try:
            batch["plan_snapshots"] = json.loads(batch.pop("plan_snapshots_json"))
        except (json.JSONDecodeError, TypeError):
            batch["plan_snapshots"] = {}
        if not batch.get("device_udid"):
            for attempt in attempt_items:
                run_id = attempt.get("run_id")
                if run_id:
                    result_path = Path(self.database_path).parent / "runs" / str(run_id) / "result.json"
                    try:
                        device = json.loads(result_path.read_text(encoding="utf-8")).get("device") or {}
                        batch["device_udid"] = device.get("udid")
                    except (OSError, json.JSONDecodeError):
                        pass
                    if batch.get("device_udid"):
                        break
        batch["metrics"] = calculate_metrics(attempt_items)
        batch["efficiency"] = calculate_efficiency(batch)
        batch["attempts"] = attempt_items if include_attempts else []
        batch["report_url"] = f"/stability-artifacts/{batch_id}/report.html"
        batch["json_url"] = f"/api/stability/batches/{batch_id}/export.json"
        return batch

    def update_efficiency_baseline(
        self, batch_id: str, payload: EfficiencyBaselineInput
    ) -> dict | None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE stability_batches SET
                   manual_execution_minutes_per_round = ?,
                   manual_report_minutes_per_round = ?,
                   automated_human_minutes_total = ?
                   WHERE batch_id = ?""",
                (
                    payload.manual_execution_minutes_per_round,
                    payload.manual_report_minutes_per_round,
                    payload.automated_human_minutes_total,
                    batch_id,
                ),
            )
        return self.get_batch(batch_id)

    def mark_batch_running(self, batch_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE stability_batches SET status = 'running', started_at = ?, finished_at = NULL, stop_requested = 0, error_message = '' WHERE batch_id = ?",
                (_utc_now(), batch_id),
            )

    def abort_batch(self, batch_id: str, message: str) -> None:
        """Restore an unstarted batch or interrupt one with completed/active attempts."""
        with self._connect() as connection:
            started = connection.execute(
                "SELECT COUNT(*) FROM stability_attempts WHERE batch_id = ? AND status != 'queued'",
                (batch_id,),
            ).fetchone()[0]
            if started == 0:
                connection.execute(
                    "UPDATE stability_batches SET status = 'draft', started_at = NULL, finished_at = NULL, error_message = ? WHERE batch_id = ? AND status = 'running'",
                    (message, batch_id),
                )
                return
            connection.execute(
                "UPDATE stability_attempts SET status = 'interrupted', classification = 'ENVIRONMENT_EXCEPTION', message = ?, finished_at = ? WHERE batch_id = ? AND status = 'running'",
                (message, _utc_now(), batch_id),
            )
            connection.execute(
                "UPDATE stability_attempts SET status = 'cancelled', message = ? WHERE batch_id = ? AND status = 'queued'",
                ("批次中断，未执行", batch_id),
            )
            connection.execute(
                "UPDATE stability_batches SET status = 'interrupted', finished_at = ?, error_message = ? WHERE batch_id = ? AND status = 'running'",
                (_utc_now(), message, batch_id),
            )

    def set_batch_error(self, batch_id: str, message: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE stability_batches SET error_message = ? WHERE batch_id = ?",
                (message, batch_id),
            )

    def finish_batch(self, batch_id: str, status: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE stability_batches SET status = ?, finished_at = ? WHERE batch_id = ?",
                (status, _utc_now(), batch_id),
            )

    def request_stop(self, batch_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE stability_batches SET stop_requested = 1 WHERE batch_id = ? AND status = 'running'",
                (batch_id,),
            )

    def should_stop(self, batch_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT stop_requested FROM stability_batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
        return bool(row and row["stop_requested"])

    def queued_attempts(self, batch_id: str) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM stability_attempts WHERE batch_id = ? AND status = 'queued' ORDER BY sequence_no",
                (batch_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def start_attempt(self, attempt_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE stability_attempts SET status = 'running', started_at = ? "
                "WHERE attempt_id = ? AND status = 'queued'",
                (_utc_now(), attempt_id),
            )
            return cursor.rowcount == 1

    def complete_attempt(self, attempt_id: int, result: dict) -> None:
        events = result.get("events") or []
        steps = result.get("steps") or []
        locator_failures = sum(1 for event in events if event.get("type") == "ai_repair_started")
        ai_interventions = locator_failures
        ai_successes = sum(1 for event in events if event.get("type") == "ai_repair_succeeded")
        ai_steps = sum(1 for step in steps if step.get("ai_repair_used"))
        duration = _duration_seconds(result.get("started_at"), result.get("finished_at"))
        with self._connect() as connection:
            connection.execute(
                """UPDATE stability_attempts SET
                   status = 'completed', run_id = ?, classification = ?, message = ?,
                   finished_at = ?, duration_seconds = ?, locator_failures = ?,
                   ai_interventions = ?, ai_repairs_succeeded = ?, step_count = ?, ai_step_count = ?,
                   environment_retry_history_json = ?
                   WHERE attempt_id = ?""",
                (
                    result.get("run_id"),
                    result.get("classification", "ENVIRONMENT_EXCEPTION"),
                    str(result.get("message") or ""),
                    result.get("finished_at") or _utc_now(),
                    duration,
                    locator_failures,
                    ai_interventions,
                    ai_successes,
                    len(steps),
                    ai_steps,
                    json.dumps(result.get("environment_retry_history") or [], ensure_ascii=False),
                    attempt_id,
                ),
            )

    def fail_attempt(self, attempt_id: int, message: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE stability_attempts SET status = 'completed',
                   classification = 'ENVIRONMENT_EXCEPTION', message = ?, finished_at = ?
                   WHERE attempt_id = ?""",
                (message, _utc_now(), attempt_id),
            )

    def cancel_queued(
        self, batch_id: str, reason: str = "用户请求安全停止，未执行"
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE stability_attempts SET status = 'cancelled', message = ? WHERE batch_id = ? AND status = 'queued'",
                (reason, batch_id),
            )

    def cancel_case_queued(
        self, batch_id: str, plan_record_id: int, reason: str
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE stability_attempts SET status = 'cancelled', message = ? "
                "WHERE batch_id = ? AND plan_record_id = ? AND status = 'queued'",
                (reason, batch_id, plan_record_id),
            )


class StabilityBatchManager:
    def __init__(
        self,
        stability_repository: StabilityRepository,
        plan_repository: PlanRepository,
        run_once: Callable,
        report_renderer: Callable[[dict], None],
        repair_patch_store: RepairPatchStore | None = None,
        batch_session: Callable | None = None,
        reserve_device: Callable[[], Callable[[], None]] | None = None,
        on_batch_finished: Callable[[dict], None] | None = None,
        environment_recovery_check: Callable[[DevicePlatform, str | None], bool] | None = None,
    ):
        self.repository = stability_repository
        self.plan_repository = plan_repository
        self.run_once = run_once
        self.report_renderer = report_renderer
        self.repair_patch_store = repair_patch_store
        self.batch_session = batch_session
        self.reserve_device = reserve_device
        self.on_batch_finished = on_batch_finished
        self.environment_recovery_check = environment_recovery_check
        self._lock = threading.Lock()
        self._active_batch_id: str | None = None
        self._reservation_release: Callable[[], None] | None = None

    @property
    def active_batch_id(self) -> str | None:
        with self._lock:
            return self._active_batch_id

    def start(self, batch_id: str) -> dict:
        batch = self.repository.get_batch(batch_id)
        if batch is None:
            raise KeyError(batch_id)
        if batch["status"] != "draft":
            raise RuntimeError("只有待执行批次可以启动")
        with self._lock:
            if self._active_batch_id is not None:
                raise RuntimeError("当前已有稳定性批次正在占用测试设备")
            self._active_batch_id = batch_id
            reservation = None
            try:
                if self.reserve_device is not None:
                    reservation = self.reserve_device()
                    self._reservation_release = reservation
                self.repository.mark_batch_running(batch_id)
                thread = threading.Thread(target=self._run, args=(batch_id,), daemon=True)
                thread.start()
            except Exception as exc:
                if reservation is not None:
                    reservation()
                    self._reservation_release = None
                self.repository.abort_batch(batch_id, f"批次启动失败：{type(exc).__name__}: {exc}")
                self._active_batch_id = None
                raise RuntimeError(f"批次启动失败：{exc}") from exc
        return self.repository.get_batch(batch_id)

    def stop(self, batch_id: str) -> dict:
        batch = self.repository.get_batch(batch_id)
        if batch is None:
            raise KeyError(batch_id)
        if batch["status"] != "running":
            raise RuntimeError("该批次当前未运行")
        self.repository.request_stop(batch_id)
        return self.repository.get_batch(batch_id)

    def _run(self, batch_id: str) -> None:
        final_status = "completed"
        try:
            batch = self.repository.get_batch(batch_id)
            if batch is None:
                raise KeyError(batch_id)
            platform = DevicePlatform(batch["platform"])
            context = (
                self.batch_session(platform, batch.get("device_udid"))
                if self.batch_session is not None
                else nullcontext(None)
            )
            with context as batch_run:
                for attempt in self.repository.queued_attempts(batch_id):
                    if self.repository.should_stop(batch_id):
                        self.repository.cancel_queued(batch_id)
                        final_status = "stopped"
                        break
                    if not self.repository.start_attempt(attempt["attempt_id"]):
                        continue
                    record = self.plan_repository.get(attempt["plan_record_id"])
                    if record is None:
                        self.repository.fail_attempt(attempt["attempt_id"], "执行计划已不存在")
                        continue
                    try:
                        frozen_plan = batch.get("plan_snapshots", {}).get(
                            str(record.record_id)
                        )
                        execution_record = (
                            record.model_copy(
                                update={"plan": ExecutionPlan.model_validate(frozen_plan)}
                            )
                            if frozen_plan
                            else record
                        )
                        def execute_attempt() -> dict:
                            if batch_run is not None:
                                return batch_run(execution_record)
                            parameters = inspect.signature(self.run_once).parameters
                            if len(parameters) >= 3:
                                return self.run_once(execution_record, platform, batch.get("device_udid"))
                            return self.run_once(execution_record, platform)

                        result = execute_attempt()
                        if (
                            result.get("classification") in ENVIRONMENT_FAILURES
                            and self.environment_recovery_check is not None
                            and self.environment_recovery_check(platform, batch.get("device_udid"))
                        ):
                            first_failure = {
                                "classification": result.get("classification"),
                                "message": result.get("message", ""),
                                "run_id": result.get("run_id"),
                                "finished_at": result.get("finished_at"),
                            }
                            retry_result = execute_attempt()
                            retry_history = list(result.get("environment_retry_history") or [])
                            retry_history.append(first_failure)
                            retry_result["environment_retry_history"] = retry_history
                            if retry_result.get("status") == "passed" and retry_result.get("classification") in PRODUCT_PASS:
                                retry_result["classification"] = "ENVIRONMENT_RECOVERED_PASS"
                                retry_result["message"] = "环境异常经设备连接检查后仅补跑一次，原验收规则通过"
                            result = retry_result
                        self.repository.complete_attempt(attempt["attempt_id"], result)
                        if self.repair_patch_store is not None:
                            for patch in verified_patches_from_result(
                                record, result, batch_id=batch_id
                            ):
                                self.repair_patch_store.save_verified(patch)
                        if (
                            result.get("status") == "passed"
                            and record.plan.status != PlanStatus.VALIDATED
                            and record.plan.status != PlanStatus.CANDIDATE
                        ):
                            plan = record.plan.model_copy(deep=True)
                            plan.status = PlanStatus.VALIDATED
                            self.plan_repository.update(record.record_id, plan, record.validation)
                        classification = str(result.get("classification") or "")
                        if classification in ENVIRONMENT_FAILURES:
                            self.repository.cancel_queued(
                                batch_id, "设备或环境异常，暂停本批剩余动作"
                            )
                            final_status = "interrupted"
                            break
                        if classification and classification not in PRODUCT_PASS:
                            self.repository.cancel_case_queued(
                                batch_id,
                                record.record_id,
                                f"本用例前序轮次未通过（{classification}），跳过剩余轮次",
                            )
                    except Exception as exc:
                        self.repository.fail_attempt(
                            attempt["attempt_id"], f"{type(exc).__name__}: {exc}"
                        )
            self.repository.finish_batch(batch_id, final_status)
            detail = self.repository.get_batch(batch_id)
            if detail is not None:
                try:
                    self.report_renderer(detail)
                except Exception as exc:
                    self.repository.set_batch_error(
                        batch_id, f"报告生成失败：{type(exc).__name__}: {exc}"
                    )
        except Exception as exc:
            self.repository.abort_batch(batch_id, f"批次中断：{type(exc).__name__}: {exc}")
        finally:
            release = self._reservation_release
            self._reservation_release = None
            if release is not None:
                release()
            with self._lock:
                self._active_batch_id = None
            if self.on_batch_finished is not None:
                detail = self.repository.get_batch(batch_id)
                if detail is not None:
                    try:
                        self.on_batch_finished(detail)
                    except Exception:
                        pass


def calculate_metrics(attempts: list[dict]) -> dict:
    total = len(attempts)
    completed = [item for item in attempts if item.get("status") == "completed"]
    classifications = [item.get("classification") for item in completed if item.get("classification")]
    automation_completed = sum(1 for value in classifications if value in AUTOMATION_COMPLETED)
    product_passes = sum(1 for value in classifications if value in PRODUCT_PASS)
    product_failures = sum(
        1 for value in classifications if value in {"PRODUCT_FAILURE", "ACCEPTANCE_MISMATCH"}
    )
    automation_failures = sum(1 for value in classifications if value in AUTOMATION_FAILURES)
    environment_failures = sum(1 for value in classifications if value in ENVIRONMENT_FAILURES)
    test_data_failures = sum(1 for value in classifications if value in TEST_DATA_FAILURES)
    durations = [float(item["duration_seconds"]) for item in completed if item.get("duration_seconds") is not None]
    ai_interventions = sum(int(item.get("ai_interventions") or 0) for item in completed)
    ai_successes = sum(int(item.get("ai_repairs_succeeded") or 0) for item in completed)
    steps = sum(int(item.get("step_count") or 0) for item in completed)
    ai_steps = sum(int(item.get("ai_step_count") or 0) for item in completed)
    counts: dict[str, int] = {}
    for value in classifications:
        counts[value] = counts.get(value, 0) + 1
    return {
        "total_attempts": total,
        "finished_attempts": len(completed),
        "automation_completed": automation_completed,
        "automation_completion_rate": _rate(automation_completed, len(completed)),
        "product_passes": product_passes,
        "product_failures": product_failures,
        "acceptance_mismatches": product_failures,
        "product_pass_rate": _rate(product_passes, automation_completed),
        "average_duration_seconds": round(sum(durations) / len(durations), 2) if durations else 0,
        "locator_failures": sum(int(item.get("locator_failures") or 0) for item in completed),
        "ai_interventions": ai_interventions,
        "ai_repairs_succeeded": ai_successes,
        "ai_repair_success_rate": _rate(ai_successes, ai_interventions),
        "deterministic_action_rate": _rate(steps - ai_steps, steps),
        "automation_failures": automation_failures,
        "environment_failures": environment_failures,
        "test_data_failures": test_data_failures,
        "classification_counts": counts,
    }


def calculate_efficiency(batch: dict) -> dict:
    execution = batch.get("manual_execution_minutes_per_round")
    reporting = batch.get("manual_report_minutes_per_round")
    automated = batch.get("automated_human_minutes_total")
    if execution is None or reporting is None or automated is None:
        return {"configured": False}
    manual_total = (float(execution) + float(reporting)) * int(batch["repetitions"])
    saved = manual_total - float(automated)
    return {
        "configured": True,
        "manual_minutes_total": round(manual_total, 2),
        "automated_human_minutes_total": round(float(automated), 2),
        "human_minutes_saved": round(saved, 2),
        "human_time_saving_rate": round(saved / manual_total * 100, 1) if manual_total else 0.0,
    }


def render_stability_report(detail: dict, output_root: Path, template_root: Path) -> Path:
    target_dir = output_root / detail["batch_id"]
    target_dir.mkdir(parents=True, exist_ok=True)
    environment = Environment(
        loader=FileSystemLoader(template_root),
        autoescape=select_autoescape(["html"]),
    )
    target = target_dir / "report.html"
    target.write_text(
        environment.get_template("stability_report.html").render(batch=detail),
        encoding="utf-8",
    )
    (target_dir / "result.json").write_text(
        json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return target


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator * 100, 1) if denominator else 0.0


def _duration_seconds(started_at: str | None, finished_at: str | None) -> float | None:
    if not started_at or not finished_at:
        return None
    try:
        started = datetime.fromisoformat(started_at)
        finished = datetime.fromisoformat(finished_at)
        return round((finished - started).total_seconds(), 3)
    except ValueError:
        return None
