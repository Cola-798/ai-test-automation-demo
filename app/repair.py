from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from .locator_catalog import resolve_target
from .schemas import ExecutionPlan, Locator, LocatorStrategy, PlanRecord

PatchStatus = Literal["VERIFIED", "PROMOTED", "REJECTED"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def plan_execution_hash(plan: ExecutionPlan) -> str:
    """Identify instructions and the effective locator rules without approval metadata."""
    target_keys = sorted({
        key
        for item in [*plan.steps, *plan.oracles]
        if (key := item.target_key)
    })
    resolved_catalog = {}
    for key in target_keys:
        resolved_catalog[key] = {}
        for platform in ("android", "ios"):
            try:
                locators = resolve_target(key, platform)
            except LookupError:
                # A target can be intentionally configured for only one platform.
                # Keep the missing platform deterministic in the hash rather than
                # blocking execution of the platform that does have a locator.
                locators = ()
            resolved_catalog[key][platform] = [
                locator.model_dump(mode="json") for locator in locators
            ]
    content = {
        "steps": [step.model_dump(mode="json") for step in plan.steps],
        "oracles": [oracle.model_dump(mode="json") for oracle in plan.oracles],
        "state_contract": plan.state_contract.model_dump(mode="json"),
        "test_parameters": {
            key: rule.model_dump(mode="json")
            for key, rule in sorted(plan.test_parameters.items())
        },
        "locator_snapshot": {
            key: [locator.model_dump(mode="json") for locator in locators]
            for key, locators in sorted(plan.locator_snapshot.items())
        },
        "resolved_locator_catalog": resolved_catalog,
    }
    return hashlib.sha256(
        json.dumps(content, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class RepairPatch:
    patch_id: str
    case_id: str
    source_plan_record_id: int
    step_id: str
    device_udid: str
    platform: str
    locator: Locator
    source_run_id: str
    proposal_hash: str
    status: PatchStatus = "VERIFIED"
    batch_id: str = ""
    source_plan_hash: str = ""


class RepairPatchStore:
    """Persist device-scoped, oracle-verified repair locators."""

    def __init__(self, database_path: Path):
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS repair_patches (
                    patch_id TEXT PRIMARY KEY, case_id TEXT NOT NULL,
                    source_plan_record_id INTEGER NOT NULL, step_id TEXT NOT NULL,
                    device_udid TEXT NOT NULL, platform TEXT NOT NULL,
                    locator_json TEXT NOT NULL, source_run_id TEXT NOT NULL,
                    proposal_hash TEXT NOT NULL, status TEXT NOT NULL,
                    batch_id TEXT NOT NULL DEFAULT '',
                    source_plan_hash TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, promoted_at TEXT,
                    UNIQUE(case_id, step_id, device_udid, platform, proposal_hash, batch_id,
                           source_plan_record_id, source_plan_hash)
                )"""
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(repair_patches)").fetchall()
            }
            if "batch_id" not in columns:
                connection.execute(
                    "ALTER TABLE repair_patches ADD COLUMN batch_id TEXT NOT NULL DEFAULT ''"
                )
            if "source_plan_hash" not in columns:
                connection.execute(
                    "ALTER TABLE repair_patches ADD COLUMN source_plan_hash TEXT NOT NULL DEFAULT ''"
                )
            self._migrate_source_scoped_uniqueness(connection)

    @staticmethod
    def _migrate_source_scoped_uniqueness(connection: sqlite3.Connection) -> None:
        legacy_unique = False
        required = {"case_id", "step_id", "device_udid", "platform", "proposal_hash"}
        for index in connection.execute("PRAGMA index_list(repair_patches)").fetchall():
            if not index["unique"]:
                continue
            columns = [
                row["name"]
                for row in connection.execute(
                    f"PRAGMA index_info({json.dumps(index['name'])})"
                ).fetchall()
            ]
            if required.issubset(columns) and not {
                "source_plan_record_id", "source_plan_hash"
            }.issubset(columns):
                legacy_unique = True
                break
        if not legacy_unique:
            return
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("ALTER TABLE repair_patches RENAME TO repair_patches_legacy_source_key")
        connection.execute(
            """CREATE TABLE repair_patches (
                patch_id TEXT PRIMARY KEY, case_id TEXT NOT NULL,
                source_plan_record_id INTEGER NOT NULL, step_id TEXT NOT NULL,
                device_udid TEXT NOT NULL, platform TEXT NOT NULL,
                locator_json TEXT NOT NULL, source_run_id TEXT NOT NULL,
                proposal_hash TEXT NOT NULL, status TEXT NOT NULL,
                batch_id TEXT NOT NULL DEFAULT '', source_plan_hash TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, promoted_at TEXT,
                UNIQUE(case_id, step_id, device_udid, platform, proposal_hash, batch_id,
                       source_plan_record_id, source_plan_hash)
            )"""
        )
        connection.execute(
            """INSERT INTO repair_patches
               (patch_id, case_id, source_plan_record_id, step_id, device_udid, platform,
                locator_json, source_run_id, proposal_hash, status, batch_id, source_plan_hash,
                created_at, promoted_at)
               SELECT patch_id, case_id, source_plan_record_id, step_id, device_udid, platform,
                      locator_json, source_run_id, proposal_hash, status, batch_id, source_plan_hash,
                      created_at, promoted_at
               FROM repair_patches_legacy_source_key"""
        )
        connection.execute("DROP TABLE repair_patches_legacy_source_key")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    def save_verified(self, patch: RepairPatch) -> RepairPatch:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM repair_patches WHERE case_id=? AND step_id=?
                   AND device_udid=? AND platform=? AND proposal_hash=? AND batch_id=?
                   AND source_plan_record_id=? AND source_plan_hash=?""",
                (patch.case_id, patch.step_id, patch.device_udid, patch.platform,
                 patch.proposal_hash, patch.batch_id, patch.source_plan_record_id,
                 patch.source_plan_hash),
            ).fetchone()
            if row:
                return self._to_patch(row)
            connection.execute(
                """INSERT INTO repair_patches(patch_id, case_id, source_plan_record_id,
                   step_id, device_udid, platform, locator_json, source_run_id,
                   proposal_hash, status, batch_id, source_plan_hash, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (patch.patch_id, patch.case_id, patch.source_plan_record_id, patch.step_id,
                 patch.device_udid, patch.platform, patch.locator.model_dump_json(),
                 patch.source_run_id, patch.proposal_hash, patch.status, patch.batch_id,
                 patch.source_plan_hash, _now()),
            )
        return patch

    def list_for_case(
        self,
        case_id: str,
        device_udid: str,
        platform: str,
        *,
        batch_id: str = "",
        include_verified: bool = False,
        source_plan_record_id: int | None = None,
        source_plan_hash: str = "",
    ) -> list[RepairPatch]:
        if not include_verified and (source_plan_record_id is None or not source_plan_hash):
            raise ValueError("执行补丁前必须指定来源计划和执行内容")
        visibility = "status IN ('VERIFIED','PROMOTED')" if include_verified else "(status='PROMOTED' OR (status='VERIFIED' AND batch_id=?))"
        parameters = (case_id, device_udid, platform) if include_verified else (case_id, device_udid, platform, batch_id)
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT * FROM repair_patches WHERE case_id=? AND device_udid=?
                   AND platform=? AND {visibility} ORDER BY created_at DESC""",
                parameters,
            ).fetchall()
        latest: dict[str, RepairPatch] = {}
        for row in rows:
            patch = self._to_patch(row)
            if source_plan_record_id is not None and patch.source_plan_record_id != source_plan_record_id:
                continue
            if source_plan_hash and patch.source_plan_hash != source_plan_hash:
                continue
            latest.setdefault(patch.step_id, patch)
        return list(latest.values())

    def mark_promoted(self, patch_ids: list[str]) -> None:
        if not patch_ids:
            return
        placeholders = ",".join("?" for _ in patch_ids)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE repair_patches SET status='PROMOTED', promoted_at=? WHERE patch_id IN ({placeholders})",
                (_now(), *patch_ids),
            )

    @staticmethod
    def _to_patch(row: sqlite3.Row) -> RepairPatch:
        return RepairPatch(
            row["patch_id"], row["case_id"], int(row["source_plan_record_id"]),
            row["step_id"], row["device_udid"], row["platform"],
            Locator.model_validate_json(row["locator_json"]), row["source_run_id"],
            row["proposal_hash"], row["status"], row["batch_id"], row["source_plan_hash"],
        )


def verified_patches_from_result(
    record: PlanRecord,
    result: dict,
    *,
    batch_id: str = "",
) -> list[RepairPatch]:
    """Create patches only after every original hard oracle passed."""
    if result.get("status") != "passed" or not result.get("oracles"):
        return []
    expected_oracles = {oracle.oracle_id for oracle in record.plan.oracles}
    passed_oracles = {
        str(item.get("oracle_id")) for item in result["oracles"]
        if item.get("oracle_id") and item.get("passed")
    }
    if any(not item.get("passed") for item in result["oracles"]) or passed_oracles != expected_oracles:
        return []
    device = result.get("device") or {}
    device_udid = str(device.get("udid") or "")
    platform = str(device.get("platform") or "android").lower()
    if not device_udid:
        return []
    patches: list[RepairPatch] = []
    for event in result.get("events") or []:
        if event.get("type") != "ai_repair_succeeded":
            continue
        proposal = event.get("proposal") or {}
        if proposal.get("x") is None or proposal.get("y") is None:
            continue
        locator = Locator(
            strategy=LocatorStrategy.NORMALIZED_COORDINATE,
            value=f"{float(proposal['x'])},{float(proposal['y'])}",
        )
        digest = hashlib.sha256(
            json.dumps({"source_plan_record_id": record.record_id,
                        "source_plan_hash": plan_execution_hash(record.plan),
                        "step_id": event.get("step_id"),
                        "locator": locator.model_dump(mode="json")}, sort_keys=True).encode()
        ).hexdigest()
        patch = RepairPatch(
            f"patch-{uuid4().hex[:12]}", record.plan.source.case_id, record.record_id,
            str(event.get("step_id") or ""), device_udid, platform, locator,
            str(result.get("run_id") or ""), digest, "VERIFIED", batch_id,
            plan_execution_hash(record.plan),
        )
        if patch.step_id and patch.source_run_id:
            patches.append(patch)
    return patches


def apply_runtime_patches(plan: ExecutionPlan, patches: dict[str, Locator]) -> ExecutionPlan:
    if not patches:
        return plan
    updated = plan.model_copy(deep=True)
    patched_steps = []
    for step in updated.steps:
        if step.step_id not in patches:
            patched_steps.append(step)
            continue
        metadata = dict(step.metadata)
        if step.target_key:
            metadata["patched_target_key"] = step.target_key
        patched_steps.append(
            step.model_copy(
                update={
                    "locator": patches[step.step_id],
                    "target_key": None,
                    "metadata": metadata,
                    "allow_ai_repair": False,
                }
            )
        )
    updated.steps = patched_steps
    return updated


def patched_record(record: PlanRecord, patches: list[RepairPatch]) -> PlanRecord:
    source_hash = plan_execution_hash(record.plan)
    applicable = {
        patch.step_id: patch.locator for patch in patches
        if patch.source_plan_record_id == record.record_id
        and patch.source_plan_hash == source_hash
        and patch.status in {"VERIFIED", "PROMOTED"}
    }
    plan = apply_runtime_patches(record.plan, applicable)
    return record.model_copy(update={"plan": plan})


def promote_plan_version(
    record: PlanRecord,
    patches: list[RepairPatch],
    reviewer: str,
    *,
    version: int | None = None,
) -> ExecutionPlan:
    plan = patched_record(record, patches).plan
    plan.version = version if version is not None else plan.version + 1
    plan.qa_confirmed_by = reviewer
    plan.qa_confirmed_at = _now()
    plan.compiler_mode = "repair_promoted"
    return plan
