from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .schemas import ExecutionPlan


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class UpgradeCandidateRepository:
    """Keep repairs isolated until the required execution lane has passed."""

    def __init__(self, database_path: Path):
        self.database_path = database_path
        with sqlite3.connect(database_path) as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS upgrade_candidates (
                candidate_id TEXT PRIMARY KEY, case_id TEXT NOT NULL,
                source_record_id INTEGER NOT NULL, candidate_record_id INTEGER,
                device_udid TEXT NOT NULL, platform TEXT NOT NULL,
                reviewer TEXT NOT NULL, source_plan_hash TEXT NOT NULL,
                plan_json TEXT NOT NULL, content_hash TEXT NOT NULL,
                patch_ids_json TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL,
                first_run_id TEXT, stability_batch_id TEXT,
                error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(source_record_id, device_udid, content_hash))""")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(upgrade_candidates)")}
            if "patch_ids_json" not in columns:
                connection.execute("ALTER TABLE upgrade_candidates ADD COLUMN patch_ids_json TEXT NOT NULL DEFAULT '[]'")
            if "source_plan_hash" not in columns:
                connection.execute("ALTER TABLE upgrade_candidates ADD COLUMN source_plan_hash TEXT NOT NULL DEFAULT ''")
            for name, definition in {
                "source_run_id": "TEXT NOT NULL DEFAULT ''",
                "validation_lane": "TEXT NOT NULL DEFAULT 'stability'",
                "required_repetitions": "INTEGER NOT NULL DEFAULT 1",
                "analysis_json": "TEXT NOT NULL DEFAULT '{}'",
            }.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE upgrade_candidates ADD COLUMN {name} {definition}")

    def create(self, *, case_id: str, source_record_id: int, device_udid: str,
               platform: str, reviewer: str, plan: ExecutionPlan, content_hash: str,
               source_plan_hash: str, patch_ids: list[str] | None = None,
               source_run_id: str = "", validation_lane: str = "stability",
               required_repetitions: int = 1, analysis: dict | None = None) -> dict:
        now = _now()
        candidate_id = f"upgrade-{uuid4().hex[:12]}"
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT * FROM upgrade_candidates WHERE source_record_id=? AND device_udid=? AND content_hash=?",
                (source_record_id, device_udid, content_hash),
            ).fetchone()
            if row:
                existing = self.get(row[0])
                if existing["source_run_id"] != source_run_id or existing["validation_lane"] != validation_lane:
                    raise ValueError("相同修复已有其他来源的候选，请先完成已有候选")
                return existing
            connection.execute(
                """INSERT INTO upgrade_candidates
                   (candidate_id, case_id, source_record_id, device_udid, platform, reviewer,
                    source_plan_hash, plan_json, content_hash, patch_ids_json, status, created_at, updated_at,
                    source_run_id, validation_lane, required_repetitions, analysis_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'awaiting_first_run', ?, ?, ?, ?, ?, ?)""",
                (candidate_id, case_id, source_record_id, device_udid, platform, reviewer,
                 source_plan_hash, plan.model_dump_json(), content_hash, json.dumps(patch_ids or []), now, now,
                 source_run_id, validation_lane, required_repetitions, json.dumps(analysis or {}, ensure_ascii=False)),
            )
        return self.get(candidate_id)

    def get(self, candidate_id: str) -> dict | None:
        with sqlite3.connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM upgrade_candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
        return self._decode(row) if row else None

    def list(self) -> list[dict]:
        with sqlite3.connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute("SELECT * FROM upgrade_candidates ORDER BY created_at DESC").fetchall()
        return [self._decode(row) for row in rows]

    def _decode(self, row) -> dict:
        item = dict(row)
        item["plan"] = ExecutionPlan.model_validate_json(item.pop("plan_json"))
        item["patch_ids"] = json.loads(item.pop("patch_ids_json") or "[]")
        item["analysis"] = json.loads(item.pop("analysis_json") or "{}")
        return item

    def mark_first_run(self, candidate_id: str, run_id: str, passed: bool,
                       candidate_record_id: int | None, error: str = "") -> dict | None:
        candidate = self.get(candidate_id)
        state = ("awaiting_activation" if candidate["validation_lane"] == "execution" else "awaiting_stability") if passed else "failed_first_run"
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """UPDATE upgrade_candidates SET status=?, first_run_id=?, candidate_record_id=?,
                   error=?, updated_at=?, plan_json=COALESCE((SELECT plan_json FROM plans WHERE record_id=?), plan_json)
                   WHERE candidate_id=? AND status='first_run_running'""",
                (state, run_id, candidate_record_id, error, _now(), candidate_record_id, candidate_id),
            )
        return self.get(candidate_id)

    def claim_first_run(self, candidate_id: str) -> bool:
        with sqlite3.connect(self.database_path) as connection:
            cursor = connection.execute(
                "UPDATE upgrade_candidates SET status='first_run_running', stability_batch_id=NULL, updated_at=? WHERE candidate_id=? AND status IN ('awaiting_first_run', 'failed_first_run', 'failed_stability')",
                (_now(), candidate_id),
            )
        return cursor.rowcount == 1

    def release_first_run_claim(self, candidate_id: str) -> None:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE upgrade_candidates SET status='awaiting_first_run', updated_at=? WHERE candidate_id=? AND status='first_run_running'",
                (_now(), candidate_id),
            )

    def attach_batch(self, candidate_id: str, batch_id: str) -> dict | None:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE upgrade_candidates SET stability_batch_id=?, updated_at=? WHERE candidate_id=? AND status='awaiting_stability'",
                (batch_id, _now(), candidate_id),
            )
        return self.get(candidate_id)

    def reject_activation(self, candidate_id: str, error: str) -> None:
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "UPDATE upgrade_candidates SET status='stale', error=?, updated_at=? WHERE candidate_id=? AND status='awaiting_activation'",
                (error, _now(), candidate_id),
            )

    def finish_batch(self, batch_id: str, *, passed: bool, error: str = "") -> dict | None:
        status = "activated" if passed else "failed_stability"
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT candidate_id FROM upgrade_candidates WHERE stability_batch_id=? AND status='awaiting_stability'",
                (batch_id,),
            ).fetchone()
            if not row:
                return None
            connection.execute(
                "UPDATE upgrade_candidates SET status=?, error=?, updated_at=? WHERE candidate_id=?",
                (status, error, _now(), row[0]),
            )
        return self.get(row[0])

    def finish_candidate(self, candidate_id: str, batch_id: str, *, passed: bool, error: str = "") -> dict | None:
        status = "activated" if passed else "failed_stability"
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                """UPDATE upgrade_candidates SET status=?, error=?, updated_at=?
                   WHERE candidate_id=? AND stability_batch_id=? AND status='awaiting_stability'""",
                (status, error, _now(), candidate_id, batch_id),
            )
        return self.get(candidate_id)

    def activate_transactionally(
        self, candidate_id: str, batch_id: str | None, *,
        source_plan_hash: str, candidate_content_hash: str, validated_plan_json: str,
    ) -> bool:
        """Atomically publish plan status, candidate state, and its repair patches."""
        now = _now()
        with sqlite3.connect(self.database_path, timeout=15) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT source_plan_hash, content_hash, candidate_record_id, patch_ids_json, source_record_id
                   FROM upgrade_candidates WHERE candidate_id=? AND stability_batch_id IS ?
                   AND status=?""",
                (candidate_id, batch_id, "awaiting_stability" if batch_id else "awaiting_activation"),
            ).fetchone()
            if not row or row[0] != source_plan_hash or row[1] != candidate_content_hash or row[2] is None:
                return False
            from .repair import plan_execution_hash

            source_row = connection.execute(
                "SELECT plan_json FROM plans WHERE record_id=?", (row[4],)
            ).fetchone()
            if not source_row or plan_execution_hash(ExecutionPlan.model_validate_json(source_row[0])) != source_plan_hash:
                return False
            source_plan = ExecutionPlan.model_validate_json(source_row[0])
            # Recheck the current formal version while holding the write lock.
            formal = []
            for record_id, plan_json in connection.execute("SELECT record_id, plan_json FROM plans WHERE status IN ('confirmed','validated')"):
                plan = ExecutionPlan.model_validate_json(plan_json)
                if plan.source.case_id == source_plan.source.case_id:
                    formal.append((plan.status == "validated", plan.version, record_id))
            if not formal or max(formal)[2] != row[4]:
                return False
            candidate_record_id = int(row[2])
            plan_row = connection.execute(
                "SELECT status, plan_json FROM plans WHERE record_id=?", (candidate_record_id,)
            ).fetchone()
            if not plan_row or plan_row[0] != "candidate":
                return False
            if plan_execution_hash(ExecutionPlan.model_validate_json(plan_row[1])) != candidate_content_hash:
                return False
            connection.execute(
                "UPDATE plans SET status='validated', plan_json=?, updated_at=? WHERE record_id=? AND status='candidate'",
                (validated_plan_json, now, candidate_record_id),
            )
            connection.execute(
                "UPDATE upgrade_candidates SET status='activated', error='', updated_at=? WHERE candidate_id=?",
                (now, candidate_id),
            )
            patch_ids = json.loads(row[3] or "[]")
            if patch_ids:
                placeholders = ",".join("?" for _ in patch_ids)
                connection.execute(
                    f"UPDATE repair_patches SET status='PROMOTED', promoted_at=? WHERE patch_id IN ({placeholders})",
                    (now, *patch_ids),
                )
        return True

    def activate_atomically(
        self, candidate_id: str, batch_id: str, record_id: int, plan: ExecutionPlan
    ) -> bool:
        now = _now()
        with sqlite3.connect(self.database_path, timeout=15) as connection:
            connection.execute("BEGIN IMMEDIATE")
            candidate = connection.execute(
                "SELECT status, stability_batch_id, candidate_record_id FROM upgrade_candidates WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
            if not candidate or candidate[0] != "awaiting_stability" or candidate[1] != batch_id or candidate[2] != record_id:
                connection.rollback()
                return False
            cursor = connection.execute(
                "UPDATE plans SET status='validated', plan_json=?, updated_at=? WHERE record_id=? AND status='candidate'",
                (plan.model_dump_json(), now, record_id),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return False
            cursor = connection.execute(
                "UPDATE upgrade_candidates SET status='activated', error='', updated_at=? WHERE candidate_id=? AND status='awaiting_stability'",
                (now, candidate_id),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return False
        return True

    def candidates_for_records(self) -> set[int]:
        with sqlite3.connect(self.database_path) as connection:
            rows = connection.execute(
                "SELECT candidate_record_id FROM upgrade_candidates WHERE status != 'activated' AND candidate_record_id IS NOT NULL"
            ).fetchall()
        return {int(row[0]) for row in rows}
