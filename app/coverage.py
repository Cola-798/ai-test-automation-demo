from __future__ import annotations

import json
from pathlib import Path

from .repository import PlanRepository
from .schemas import ExecutionPlan, PlanStatus
from .validator import validate_plan


def load_first_run_results(ledger_path: Path) -> dict[str, dict]:
    if not ledger_path.exists():
        return {}
    payload = json.loads(ledger_path.read_text(encoding="utf-8"))
    return {
        item["case_id"]: item
        for item in payload.get("latest_results", [])
        if item.get("case_id")
    }


def sync_first_run_plans(
    repository: PlanRepository,
    plans_dir: Path,
    ledger_path: Path,
) -> dict:
    results = load_first_run_results(ledger_path)
    existing_case_ids = {
        record.plan.source.case_id
        for record in repository.list()
        if not record.plan.source.case_id.startswith("CLEANUP-")
    }
    latest_files: dict[str, ExecutionPlan] = {}
    for plan_path in plans_dir.glob("*.json"):
        payload = json.loads(plan_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or "plan_id" not in payload:
            continue
        plan = ExecutionPlan.model_validate(payload)
        case_id = plan.source.case_id
        if case_id not in results:
            continue
        current = latest_files.get(case_id)
        if current is None or plan.version > current.version:
            latest_files[case_id] = plan

    imported = []
    missing_files = []
    for case_id, result in results.items():
        if case_id in existing_case_ids:
            continue
        source_plan = latest_files.get(case_id)
        if source_plan is None:
            missing_files.append(case_id)
            continue
        plan = source_plan.model_copy(deep=True)
        plan.status = PlanStatus.VALIDATED if result.get("status") == "passed" else PlanStatus.CONFIRMED
        plan.qa_confirmed_by = plan.qa_confirmed_by or "POC-QA"
        plan.qa_confirmed_at = plan.qa_confirmed_at or result.get("finished_at")
        validation = validate_plan(plan)
        if not validation.valid:
            raise ValueError(f"{case_id} 计划校验失败：{validation.model_dump()}")
        repository.save(plan, validation)
        imported.append(case_id)

    return {
        "target_cases": len(results),
        "existing_cases": len(existing_case_ids),
        "imported_cases": imported,
        "missing_plan_files": missing_files,
    }
