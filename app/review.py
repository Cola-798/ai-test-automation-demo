from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field


REVIEWABLE_CLASSIFICATIONS = {
    "PRODUCT_FAILURE",
    "ACCEPTANCE_MISMATCH",
    "AUTOMATION_FAILURE",
    "AUTOMATION_FAILURE_REPAIRABLE",
    "ENVIRONMENT_EXCEPTION",
    "TEST_DATA_FAILURE",
    "AI_REPAIR_SUCCESS",
    "AI_VISUAL_ORACLE_PASS",
}

REVIEW_OUTCOMES = {
    "PRODUCT_DEFECT": "已确认产品问题",
    "TEST_DATA_ISSUE": "测试数据问题",
    "ORACLE_ISSUE": "验收标准问题",
    "AUTOMATION_ISSUE": "自动化问题",
    "ENVIRONMENT_ISSUE": "环境问题",
    "NOT_AN_ISSUE": "非问题",
    "REPAIR_VERIFIED": "修复后通过",
}

OBSERVED_LABELS = {
    "PRODUCT_FAILURE": "历史验收不一致（待确认）",
    "ACCEPTANCE_MISMATCH": "验收不一致（待确认）",
    "AUTOMATION_FAILURE": "自动化执行失败",
    "AUTOMATION_FAILURE_REPAIRABLE": "定位失败（可修复）",
    "ENVIRONMENT_EXCEPTION": "设备或环境异常",
    "TEST_DATA_FAILURE": "测试数据异常",
    "AI_REPAIR_SUCCESS": "AI 修复后通过（待确认）",
    "AI_VISUAL_ORACLE_PASS": "AI 视觉验收通过（待确认）",
}


class ReviewUpdate(BaseModel):
    review_outcome: Literal[
        "PRODUCT_DEFECT",
        "TEST_DATA_ISSUE",
        "ORACLE_ISSUE",
        "AUTOMATION_ISSUE",
        "ENVIRONMENT_ISSUE",
        "NOT_AN_ISSUE",
        "REPAIR_VERIFIED",
    ]
    review_note: str = Field(default="", max_length=2000)
    reviewer: str = Field(min_length=1, max_length=100)


class ReviewRepository:
    def __init__(self, database_path: Path, runs_dir: Path, templates_dir: Path | None = None):
        self.database_path = database_path
        self.runs_dir = runs_dir
        self.templates_dir = templates_dir or Path(__file__).resolve().parent / "templates"
        self._result_cache_lock = threading.RLock()
        self._result_cache: dict[Path, tuple[int, int, dict]] = {}
        self._initialize()

    def run_results(self) -> list[tuple[str, dict]]:
        """Return run results while only reparsing files that changed on disk."""
        with self._result_cache_lock:
            paths = set(self.runs_dir.glob("*/result.json"))
            for stale_path in self._result_cache.keys() - paths:
                del self._result_cache[stale_path]
            for result_path in paths:
                try:
                    stat = result_path.stat()
                except OSError:
                    continue
                cached = self._result_cache.get(result_path)
                if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
                    continue
                result = _read_json(result_path)
                if result:
                    self._result_cache[result_path] = (stat.st_mtime_ns, stat.st_size, result)
                else:
                    self._result_cache.pop(result_path, None)
            return [
                (path.parent.name, cached[2])
                for path, cached in sorted(
                    self._result_cache.items(), key=lambda item: item[0].parent.name, reverse=True
                )
            ]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS result_reviews (
                    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL UNIQUE,
                    attempt_id INTEGER,
                    batch_id TEXT,
                    case_id TEXT NOT NULL,
                    case_title TEXT NOT NULL DEFAULT '',
                    plan_record_id INTEGER,
                    observed_classification TEXT NOT NULL,
                    review_status TEXT NOT NULL DEFAULT 'PENDING',
                    review_outcome TEXT,
                    review_note TEXT NOT NULL DEFAULT '',
                    reviewer TEXT NOT NULL DEFAULT '',
                    run_finished_at TEXT,
                    device_udid TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS review_reruns (
                    rerun_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    original_run_id TEXT NOT NULL,
                    run_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    classification TEXT NOT NULL,
                    report_url TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(original_run_id) REFERENCES result_reviews(run_id)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_result_reviews_status ON result_reviews(review_status)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_result_reviews_case ON result_reviews(case_id)"
            )
            self._ensure_column(connection, "result_reviews", "device_udid", "TEXT")

    @staticmethod
    def _ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def sync_from_runs(self) -> int:
        inserted = 0
        for directory_run_id, result in self.run_results():
            if directory_run_id.startswith("evidence-"):
                continue
            if str(result.get("status") or "").lower() == "passed":
                continue
            classification = str(result.get("classification") or "UNKNOWN_FAILURE")
            run_id = str(result.get("run_id") or directory_run_id)
            attempt_id, batch_id, plan_record_id = self._resolve_links(
                run_id, self.runs_dir / directory_run_id
            )
            device = result.get("device") or {}
            device_udid = str(device.get("udid") or "") or None
            now = _now()
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO result_reviews(
                        run_id, attempt_id, batch_id, case_id, case_title, plan_record_id,
                        observed_classification, run_finished_at, device_udid, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        attempt_id,
                        batch_id,
                        str(result.get("case_id") or "未知用例"),
                        str(result.get("case_title") or ""),
                        plan_record_id,
                        classification,
                        result.get("finished_at"),
                        device_udid,
                        now,
                        now,
                    ),
                )
                if device_udid:
                    connection.execute("UPDATE result_reviews SET device_udid = ? WHERE run_id = ? AND (device_udid IS NULL OR device_udid = '')", (device_udid, run_id))
                inserted += int(cursor.rowcount > 0)
        return inserted

    def _resolve_links(self, run_id: str, run_dir: Path) -> tuple[int | None, str | None, int | None]:
        with self._connect() as connection:
            attempt = connection.execute(
                "SELECT attempt_id, batch_id, plan_record_id FROM stability_attempts WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if attempt:
                return attempt["attempt_id"], attempt["batch_id"], attempt["plan_record_id"]
            plan_path = run_dir / "plan.json"
            if not plan_path.exists():
                return None, None, None
            try:
                plan = json.loads(plan_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None, None, None
            record = connection.execute(
                "SELECT record_id FROM plans WHERE plan_id = ? AND version = ? ORDER BY record_id DESC LIMIT 1",
                (plan.get("plan_id"), int(plan.get("version") or 1)),
            ).fetchone()
        return None, None, int(record["record_id"]) if record else None

    def list(
        self,
        *,
        run_ids: set[str] | None = None,
        status: str | None = None,
        keyword: str | None = None,
        observed: str | None = None,
        outcome: str | None = None,
        device_udid: str | None = None,
    ) -> list[dict]:
        self.sync_from_runs()
        if run_ids is None:
            selected = [item for item in self._latest_results_by_lane().values()
                        if str(item["result"].get("status") or "").lower() != "passed"]
            run_ids = {item["run_id"] for item in selected}
        if not run_ids:
            return []
        placeholders = ",".join("?" for _ in run_ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM result_reviews WHERE run_id IN ({placeholders}) "
                "ORDER BY COALESCE(run_finished_at, created_at) DESC",
                tuple(run_ids),
            ).fetchall()
            stability_ids = {
                row[0] for row in connection.execute(
                    f"SELECT run_id FROM stability_attempts WHERE run_id IN ({placeholders})", tuple(run_ids)
                ).fetchall()
            }
        items = []
        for row in rows:
            item = self._public_review(dict(row))
            item["occurrence_count"] = 1
            item["is_stability"] = item["run_id"] in stability_ids
            items.append(item)
        if device_udid:
            items = [item for item in items if item.get("device_udid") == device_udid]
        if status:
            items = [item for item in items if item.get("review_status") == status]
        if keyword:
            token = keyword.casefold()
            items = [
                item
                for item in items
                if token
                in " ".join(
                    (item.get("case_id") or "", item.get("case_title") or "", item.get("run_id") or "")
                ).casefold()
            ]
        if observed:
            items = [item for item in items if item.get("observed_classification") == observed]
        if outcome:
            items = [item for item in items if item.get("review_outcome") == outcome]
        return items

    def history(self, *, keyword: str | None = None, device_udid: str | None = None) -> list[dict]:
        """Return every failed run without suppressing failures that later passed."""
        self.sync_from_runs()
        failed_run_ids = {
            str(result.get("run_id") or directory_run_id)
            for directory_run_id, result in self.run_results()
            if str(result.get("status") or "").lower() != "passed"
        }
        clauses: list[str] = []
        parameters: list[object] = []
        if device_udid:
            clauses.append("device_udid = ?")
            parameters.append(device_udid)
        if keyword:
            clauses.append("(case_id LIKE ? OR case_title LIKE ? OR run_id LIKE ?)")
            token = f"%{keyword}%"
            parameters.extend([token, token, token])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM result_reviews{where} "
                "ORDER BY COALESCE(run_finished_at, created_at) DESC, review_id DESC",
                parameters,
            ).fetchall()
        return [
            self._public_review(dict(row)) | {"is_stability": row["attempt_id"] is not None}
            for row in rows
            if row["run_id"] in failed_run_ids
        ]

    def _latest_results_by_lane(self) -> dict[tuple[str, str, str], dict]:
        with self._connect() as connection:
            candidate_ids = {
                row[0] for row in connection.execute("SELECT record_id FROM plans WHERE status='candidate'")
            }
            stability_run_ids = {
                str(row[0])
                for row in connection.execute(
                    "SELECT run_id FROM stability_attempts WHERE run_id IS NOT NULL"
                ).fetchall()
            }
        latest: dict[tuple[str, str, str], dict] = {}
        for directory_run_id, result in self.run_results():
            if directory_run_id.startswith("evidence-"):
                continue
            case_id = str(result.get("case_id") or "")
            device = result.get("device") or {}
            device_key = str(device.get("udid") or device.get("name") or "")
            if not case_id or not device_key:
                continue
            run_id = str(result.get("run_id") or directory_run_id)
            if candidate_ids:
                record_id = result.get("plan_record_id")
                if record_id is None:
                    _, _, record_id = self._resolve_links(run_id, self.runs_dir / directory_run_id)
                if record_id in candidate_ids:
                    continue
            lane = "stability" if run_id in stability_run_ids else "execution"
            key = (case_id, device_key, lane)
            sort_key = (_timestamp(result.get("finished_at") or result.get("started_at")), run_id)
            current = latest.get(key)
            if current is None or sort_key > current["sort_key"]:
                latest[key] = {
                    "run_id": run_id,
                    "lane": lane,
                    "result": result,
                    "sort_key": sort_key,
                }
        return latest

    def get(self, run_id: str) -> dict | None:
        self.sync_from_runs()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT result_reviews.*,
                    (
                        SELECT COUNT(*) FROM result_reviews AS duplicate
                        WHERE duplicate.case_id = result_reviews.case_id
                          AND duplicate.observed_classification = result_reviews.observed_classification
                          AND COALESCE(duplicate.device_udid, '') = COALESCE(result_reviews.device_udid, '')
                    ) AS occurrence_count
                FROM result_reviews WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        return self._public_review(dict(row)) if row else None

    def detail(self, run_id: str) -> dict | None:
        review = self.get(run_id)
        if review is None:
            return None
        run_dir = self.runs_dir / run_id
        result = _read_json(run_dir / "result.json")
        plan = _read_json(run_dir / "plan.json")
        evidence = []
        for event in result.get("events") or []:
            screenshot = event.get("screenshot")
            if screenshot:
                evidence.append(
                    {
                        "type": "screenshot",
                        "event_id": event.get("event_id"),
                        "phase": event.get("phase"),
                        "step_id": event.get("step_id"),
                        "oracle_id": event.get("oracle_id"),
                        "label": (
                            f"{event.get('phase') or '执行'} · "
                            f"{event.get('title') or event.get('step_id') or event.get('oracle_id') or '截图'}"
                        ),
                        "url": f"/runs/{run_id}/{str(screenshot).replace('\\', '/')}",
                    }
                )
            page_source = event.get("page_source")
            if page_source:
                evidence.append({
                    "type": "xml", "event_id": event.get("event_id"),
                    "step_id": event.get("step_id"), "oracle_id": event.get("oracle_id"),
                    "label": f"失败现场 · {event.get('step_id') or event.get('oracle_id') or '页面结构'}",
                    "url": f"/runs/{run_id}/{str(page_source).replace('\\', '/')}"
                })
        if result.get("logcat"):
            evidence.append(
                {
                    "type": "log",
                    "label": "设备日志",
                    "url": f"/runs/{run_id}/{str(result['logcat']).replace('\\', '/')}",
                }
            )
        if result.get("final_page_source"):
            evidence.append(
                {
                    "type": "xml",
                    "label": "最终页面 UI XML",
                    "url": f"/runs/{run_id}/{str(result['final_page_source']).replace('\\', '/')}",
                }
            )
        ai_events = [
            event
            for event in result.get("events") or []
            if str(event.get("type") or "").startswith("ai_")
            or event.get("type") == "visual_oracle_result"
        ]
        with self._connect() as connection:
            reruns = [
                dict(row)
                for row in connection.execute(
                    "SELECT run_id, status, classification, report_url, created_at FROM review_reruns WHERE original_run_id = ? ORDER BY rerun_id DESC",
                    (run_id,),
                ).fetchall()
            ]
        return {
            "review": review,
            "source_case": plan.get("source") or {},
            "execution_plan": plan,
            "result": result,
            "evidence": evidence,
            "failed_steps": [
                step for step in result.get("steps") or [] if step.get("status") != "passed"
            ],
            "ai": {"intervened": bool(ai_events), "events": ai_events},
            "report_url": result.get("report_url") or f"/runs/{run_id}/report.html",
            "review_report_url": (
                f"/runs/{run_id}/review-report.html"
                if (run_dir / "review-report.html").exists()
                else None
            ),
            "reruns": reruns,
        }

    def update(self, run_id: str, payload: ReviewUpdate) -> dict | None:
        self.sync_from_runs()
        now = _now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE result_reviews SET review_status = 'CONFIRMED', review_outcome = ?,
                    review_note = ?, reviewer = ?, updated_at = ? WHERE run_id = ?
                """,
                (
                    payload.review_outcome,
                    payload.review_note.strip(),
                    payload.reviewer.strip(),
                    now,
                    run_id,
                ),
            )
        if not cursor.rowcount:
            return None
        detail = self.detail(run_id)
        if detail is not None:
            self._render_review_report(run_id, detail)
        return self.get(run_id)

    def _render_review_report(self, run_id: str, detail: dict) -> Path:
        environment = Environment(
            loader=FileSystemLoader(self.templates_dir),
            autoescape=select_autoescape(["html"]),
        )
        template = environment.get_template("review_report.html")
        target = self.runs_dir / run_id / "review-report.html"
        target.write_text(template.render(detail=detail), encoding="utf-8")
        return target

    def add_rerun(self, original_run_id: str, result: dict) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO review_reruns(
                    original_run_id, run_id, status, classification, report_url, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    original_run_id,
                    result["run_id"],
                    str(result.get("status") or "unknown"),
                    str(result.get("classification") or "unknown"),
                    str(result.get("report_url") or ""),
                    _now(),
                ),
            )
        detail = self.detail(original_run_id)
        if detail is not None and detail["review"]["review_status"] == "CONFIRMED":
            self._render_review_report(original_run_id, detail)

    def metrics(self, device_udid: str | None = None) -> dict:
        items = self.list(device_udid=device_udid)
        return {
            "total": len(items),
            "pending": sum(item.get("review_status") == "PENDING" for item in items),
            "execution_failures": sum(not item.get("is_stability") for item in items),
            "stability_failures": sum(bool(item.get("is_stability")) for item in items),
        }

    @staticmethod
    def _public_review(row: dict) -> dict:
        row["observed_label"] = OBSERVED_LABELS.get(
            row["observed_classification"], row["observed_classification"]
        )
        row["review_outcome_label"] = REVIEW_OUTCOMES.get(row.get("review_outcome"), "待确认")
        row["report_url"] = f"/runs/{row['run_id']}/report.html"
        row["device_udid"] = row.get("device_udid") or ""
        return row


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp(value: object) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0
