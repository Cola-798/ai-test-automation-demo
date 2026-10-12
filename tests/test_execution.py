from __future__ import annotations

from pathlib import Path

from app.runtime import run_plan
from app.schemas import ExecutionPlan


def test_confirmed_execution_plan(demo_plan_file: Path, demo_output_dir: Path) -> None:
    plan = ExecutionPlan.model_validate_json(demo_plan_file.read_text(encoding="utf-8"))
    result = run_plan(plan, demo_output_dir)
    assert result["status"] == "passed", (
        f"{result['classification']}: {result['message']}；"
        f"详细报告：{result.get('report_path', demo_output_dir / 'report.html')}"
    )
