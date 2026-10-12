from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .ai_evidence import review_run, write_rollup
from .runtime import render_report
from .schemas import ExecutionPlan


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunArtifactQueue:
    """Small SQLite-backed worker for report rendering and explicit evidence archival."""

    def __init__(self, database_path: Path, runs_dir: Path, archive_dir: Path, templates_dir: Path,
                 *, ai_review_enabled: bool = False):
        self.database_path = database_path
        self.runs_dir = runs_dir.resolve()
        self.archive_dir = archive_dir.resolve()
        self.templates_dir = templates_dir
        self.ai_review_enabled = ai_review_enabled
        self._recovering = True
        self._wake = threading.Event()
        self._stop = threading.Event()
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS artifact_jobs (
                job_id TEXT PRIMARY KEY, kind TEXT NOT NULL, run_id TEXT NOT NULL,
                status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(kind, run_id))""")
            connection.execute("UPDATE artifact_jobs SET status='queued', error=''")
        self._thread: threading.Thread | None = None
        self.recover_reports()
        self._recovering = False

    def _start_worker(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._worker, name="demo-artifact-worker", daemon=True)
        self._thread.start()

    def enqueue(self, kind: str, run_id: str) -> dict:
        if kind not in {"report", "archive", "ai_review"}:
            raise ValueError("不支持的后台任务类型")
        if kind == "ai_review" and not self.ai_review_enabled:
            raise ValueError("AI 证据核验功能尚未启用")
        if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
            raise ValueError("无效的运行 ID")
        now = _now()
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute("SELECT * FROM artifact_jobs WHERE kind=? AND run_id=?", (kind, run_id)).fetchone()
            if row and row[3] in {"queued", "running", "completed"}:
                if kind == "report" and self.ai_review_enabled and not self._recovering and not run_id.startswith("evidence-"):
                    self.enqueue("ai_review", run_id)
                return self.get(kind, run_id)
            if row:
                connection.execute("UPDATE artifact_jobs SET status='queued', error='', updated_at=? WHERE kind=? AND run_id=?", (now, kind, run_id))
            else:
                connection.execute("INSERT INTO artifact_jobs VALUES (?, ?, ?, 'queued', '', ?, ?)", (uuid4().hex, kind, run_id, now, now))
        self._start_worker()
        self._wake.set()
        if kind == "report" and self.ai_review_enabled and not self._recovering and not run_id.startswith("evidence-"):
            self.enqueue("ai_review", run_id)
        return self.get(kind, run_id)

    def get(self, kind: str, run_id: str) -> dict | None:
        with sqlite3.connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM artifact_jobs WHERE kind=? AND run_id=?", (kind, run_id)).fetchone()
        return dict(row) if row else None

    def list(self) -> list[dict]:
        with sqlite3.connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute("SELECT * FROM artifact_jobs ORDER BY created_at DESC")]

    def evidence_file(self, run_id: str, relative_path: str) -> Path:
        if Path(run_id).name != run_id:
            raise FileNotFoundError("证据不存在")
        root = (self.archive_dir / run_id).resolve()
        target = (root / relative_path).resolve()
        if root not in target.parents or not target.is_file():
            raise FileNotFoundError("证据不存在")
        return target

    def recover_reports(self) -> None:
        for result_path in self.runs_dir.glob("*/result.json"):
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not (result_path.parent / "plan.json").exists() or (result_path.parent / "report.html").exists():
                continue
            if result.get("status") in {"passed", "failed"}:
                self.enqueue("report", result_path.parent.name)

    def _worker(self) -> None:
        while not self._stop.is_set():
            with sqlite3.connect(self.database_path) as connection:
                connection.row_factory = sqlite3.Row
                row = connection.execute("SELECT * FROM artifact_jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
                if row:
                    connection.execute("UPDATE artifact_jobs SET status='running', updated_at=? WHERE job_id=? AND status='queued'", (_now(), row["job_id"]))
            if not row:
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            try:
                if row["kind"] == "report":
                    self._render_report(row["run_id"])
                elif row["kind"] == "ai_review":
                    if not self.ai_review_enabled:
                        raise ValueError("AI 证据核验功能尚未启用")
                    run_dir = self._run_dir(row["run_id"])
                    review_run(run_dir, self.archive_dir / row["run_id"])
                    write_rollup(self.runs_dir, self.database_path)
                else:
                    self._archive_evidence(row["run_id"])
                status, error = "completed", ""
            except Exception as exc:
                status, error = "failed", f"{type(exc).__name__}: {exc}"
                if row["kind"] == "ai_review":
                    try:
                        write_rollup(self.runs_dir, self.database_path)
                    except Exception:
                        pass
            with sqlite3.connect(self.database_path) as connection:
                connection.execute("UPDATE artifact_jobs SET status=?, error=?, updated_at=? WHERE job_id=?", (status, error, _now(), row["job_id"]))
            if row["kind"] == "report" and status == "failed":
                try:
                    result_path = self._run_dir(row["run_id"]) / "result.json"
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                    result["report_status"] = "failed"
                    result["report_error"] = error
                    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                except (OSError, json.JSONDecodeError):
                    pass

    def _run_dir(self, run_id: str) -> Path:
        path = (self.runs_dir / run_id).resolve()
        if path.parent != self.runs_dir or not path.is_dir():
            raise FileNotFoundError("运行记录不存在")
        return path

    def _render_report(self, run_id: str) -> None:
        run_dir = self._run_dir(run_id)
        plan = ExecutionPlan.model_validate_json((run_dir / "plan.json").read_text(encoding="utf-8"))
        result_path = run_dir / "result.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["report_status"] = "generating"
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        report_path = render_report(plan, result, run_dir)
        result["report_status"] = "completed"
        result.pop("report_error", None)
        result["report_path"] = str(report_path.resolve())
        result["report_url"] = f"/runs/{run_id}/report.html"
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    def _archive_evidence(self, run_id: str) -> None:
        run_dir = self._run_dir(run_id)
        result_path = run_dir / "result.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if result.get("report_status") != "completed":
            raise RuntimeError("报告未完成，不能归档证据")
        if not (run_dir / "report.html").is_file():
            raise RuntimeError("报告文件不存在，不能归档证据")
        target_root = (self.archive_dir / run_id).resolve()
        if target_root.parent != self.archive_dir:
            raise RuntimeError("归档目标越界")
        target_root.mkdir(parents=True, exist_ok=True)
        moved: list[tuple[Path, Path]] = []
        candidates = [run_dir / item["screenshot"] for item in result.get("events", []) if item.get("screenshot")]
        candidates.extend(path for path in run_dir.rglob("*") if path.is_file() and path.suffix.lower() in {".xml", ".log"})
        for source in dict.fromkeys(path.resolve() for path in candidates):
            if source.parent != run_dir and run_dir not in source.parents:
                raise RuntimeError("证据路径越出运行目录")
            if not source.is_file():
                continue
            relative = source.relative_to(run_dir)
            destination = target_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(destination.suffix + ".copying")
            shutil.copy2(source, temporary)
            if hashlib.sha256(source.read_bytes()).digest() != hashlib.sha256(temporary.read_bytes()).digest():
                temporary.unlink(missing_ok=True)
                raise IOError(f"归档校验失败：{relative}")
            temporary.replace(destination)
            moved.append((source, destination))
        # Only remove hot copies after every archive copy has been verified.
        for source, _ in moved:
            source.unlink()
        for event in result.get("events", []):
            if event.get("screenshot"):
                event["screenshot"] = f"/api/archive/evidence/{run_id}/{event['screenshot']}"
        result["archived"] = True
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        self._render_report(run_id)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2)
