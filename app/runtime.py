from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from .evidence_context import select_screenshots
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .device import (
    AndroidDeviceConfig,
    AppiumDeviceSession,
    configured_platform,
    device_config_for_platform,
)
from .executor import AppiumPlanExecutor, OracleFailure, PlanExecutionError, RepairOutcome
from .schemas import ActionType, DevicePlatform, ExecutionPlan, Oracle, OracleType, PlanStep, StateMode
from .visual_repair import VisualRepairService


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_ADB = Path("adb")


class StatePreparationError(RuntimeError):
    """The plan could not establish its declared business-data preconditions."""


def _capture_checkpoint(evidence, stem: str, *, page_source: bool = True) -> dict:
    checkpoint = {"checkpoint_started_at": datetime.now(timezone.utc).isoformat()}
    for key, method, suffix in (("screenshot", "capture_screenshot", "png"),
                                 ("page_source", "capture_page_source", "xml")):
        if key == "page_source" and not page_source:
            continue
        checkpoint[f"{key}_started_at"] = datetime.now(timezone.utc).isoformat()
        try:
            checkpoint[key] = getattr(evidence, method)(f"{stem}.{suffix}")
        except Exception as exc:
            warning = "失败现场页面结构采集失败" if key == "page_source" else "步骤截图采集失败"
            checkpoint["page_source_warning" if key == "page_source" else "evidence_warning"] = f"{warning}：{type(exc).__name__}: {exc}"
        checkpoint[f"{key}_finished_at"] = datetime.now(timezone.utc).isoformat()
    checkpoint["checkpoint_finished_at"] = datetime.now(timezone.utc).isoformat()
    return checkpoint


def _record_checkpoint(evidence, event: dict, stem: str) -> dict:
    previous = evidence.events[-1] if evidence.events else {}
    if (event.get("type") == "oracle_result" and previous.get("type") == "visual_oracle_result"
            and previous.get("oracle_id") == event.get("oracle_id") and previous.get("checkpoint_started_at")):
        return {key: value for key, value in previous.items() if key in {
            "screenshot", "page_source", "page_source_warning", "evidence_warning"
        } or key.startswith(("checkpoint_", "screenshot_", "page_source_"))}
    return _capture_checkpoint(evidence, stem, page_source=event.get("type") == "step_failed" or (
        event.get("type") == "oracle_result" and event.get("passed") is False))


class AdbEvidenceCollector:
    def __init__(self, run_dir: Path, *, adb: Path, udid: str):
        self.run_dir = run_dir
        self.evidence_dir = run_dir / "evidence"
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.adb = adb
        self.udid = udid
        self.events: list[dict[str, Any]] = []
        self._counter = 0
        self._event_counter = 0
        self.driver: Any | None = None

    def set_driver(self, driver: Any) -> None:
        self.driver = driver

    def clear_logcat(self) -> None:
        self._run("logcat", "-c")

    def record(self, event: dict[str, Any]) -> None:
        self._event_counter += 1
        saved = {
            **event,
            "event_id": f"event-{self._event_counter:05d}",
            "phase": event.get("phase") or "execution",
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        if event.get("type") in {"step_passed", "step_failed", "oracle_result"}:
            self._counter += 1
            evidence_key = event.get("step_id") or event.get("oracle_id") or "evidence"
            stem = f"{self._counter:03d}-{_safe_name(str(saved['phase']))}-{_safe_name(str(evidence_key))}"
            saved.update(_record_checkpoint(self, event, stem))
        self.events.append(saved)

    def capture_screenshot(self, filename: str) -> str:
        target = self.evidence_dir / filename
        completed = self._run("exec-out", "screencap", "-p", binary=True)
        target.write_bytes(completed.stdout)
        return str(target.relative_to(self.run_dir)).replace("\\", "/")

    def capture_page_source(self, filename: str) -> str:
        target = self.evidence_dir / filename
        if self.driver is not None:
            target.write_text(self.driver.page_source, encoding="utf-8")
            return str(target.relative_to(self.run_dir)).replace("\\", "/")
        remote = f"/sdcard/{_safe_name(filename)}"
        self._run("shell", "uiautomator", "dump", remote)
        completed = self._run("exec-out", "cat", remote, binary=True)
        target.write_bytes(completed.stdout)
        return str(target.relative_to(self.run_dir)).replace("\\", "/")

    def save_logcat(self) -> str:
        target = self.evidence_dir / "device-logcat.txt"
        completed = self._run("logcat", "-d", "-v", "threadtime", "-t", "1000")
        target.write_text(completed.stdout, encoding="utf-8", errors="replace")
        return str(target.relative_to(self.run_dir)).replace("\\", "/")

    def screen_size(self) -> tuple[int, int]:
        completed = self._run("shell", "wm", "size")
        sizes = re.findall(r"(\d+)x(\d+)", completed.stdout)
        if not sizes:
            raise RuntimeError(f"无法读取屏幕尺寸：{completed.stdout}")
        width, height = sizes[-1]
        return int(width), int(height)

    def tap_coordinates(self, x: int, y: int) -> None:
        if x < 0 or y < 0:
            raise ValueError("触点坐标不能为负数")
        self._run("shell", "input", "tap", str(x), str(y))

    def _run(self, *args: str, binary: bool = False) -> subprocess.CompletedProcess:
        completed = subprocess.run(
            [str(self.adb), "-s", self.udid, *args],
            capture_output=True,
            text=not binary,
            encoding=None if binary else "utf-8",
            errors=None if binary else "replace",
            timeout=20,
            check=False,
        )
        if completed.returncode != 0:
            error = completed.stderr.decode("utf-8", "replace") if binary else completed.stderr
            raise RuntimeError(f"ADB 命令失败：{' '.join(args)}；{error}")
        return completed


class IOSAppiumEvidenceCollector:
    """Collect iOS evidence through the remote XCUITest session on macOS."""

    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.evidence_dir = run_dir / "evidence"
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.events: list[dict[str, Any]] = []
        self._counter = 0
        self._event_counter = 0
        self.driver: Any | None = None

    def set_driver(self, driver: Any) -> None:
        self.driver = driver

    def clear_logcat(self) -> None:
        # iOS device logs are read from the active XCUITest session at the end.
        return None

    def record(self, event: dict[str, Any]) -> None:
        self._event_counter += 1
        saved = {
            **event,
            "event_id": f"event-{self._event_counter:05d}",
            "phase": event.get("phase") or "execution",
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        if event.get("type") in {"step_passed", "step_failed", "oracle_result"}:
            self._counter += 1
            evidence_key = event.get("step_id") or event.get("oracle_id") or "evidence"
            stem = f"{self._counter:03d}-{_safe_name(str(saved['phase']))}-{_safe_name(str(evidence_key))}"
            saved.update(_record_checkpoint(self, event, stem))
        self.events.append(saved)

    def capture_screenshot(self, filename: str) -> str:
        if self.driver is None:
            raise RuntimeError("iOS Appium 会话尚未建立")
        target = self.evidence_dir / filename
        if not self.driver.get_screenshot_as_file(str(target)):
            raise RuntimeError("XCUITest 未能保存截图")
        return str(target.relative_to(self.run_dir)).replace("\\", "/")

    def capture_page_source(self, filename: str) -> str:
        if self.driver is None:
            raise RuntimeError("iOS Appium 会话尚未建立")
        target = self.evidence_dir / filename
        target.write_text(self.driver.page_source, encoding="utf-8")
        return str(target.relative_to(self.run_dir)).replace("\\", "/")

    def save_logcat(self) -> str:
        if self.driver is None:
            raise RuntimeError("iOS Appium 会话尚未建立")
        target = self.evidence_dir / "device-syslog.json"
        logs = self.driver.get_log("syslog")
        target.write_text(json.dumps(logs, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(target.relative_to(self.run_dir)).replace("\\", "/")

    def screen_size(self) -> tuple[int, int]:
        if self.driver is None:
            raise RuntimeError("iOS Appium 会话尚未建立")
        size = self.driver.get_window_size()
        return int(size["width"]), int(size["height"])


class AppiumRuntimeSession:
    """Keep one Appium session alive for a serial stability batch."""

    def __init__(
        self,
        platform: DevicePlatform | str | None = None,
        device_udid: str | None = None,
    ):
        self.platform = DevicePlatform(platform or configured_platform())
        self.config = device_config_for_platform(self.platform, device_udid=device_udid)
        self.session = AppiumDeviceSession(self.config)
        self.driver: Any | None = None

    def prepare(self) -> Any:
        if self.driver is None:
            self.driver = self.session.open(launch_app=True)
            return self._wait_for_home(self.driver)
        try:
            self.driver.terminate_app(self.config.app_identifier)
            start_activity = getattr(self.driver, "start_activity", None)
            if callable(start_activity) and hasattr(self.config, "activity"):
                start_activity(self.config.app_identifier, self.config.activity)
            else:
                self.driver.activate_app(self.config.app_identifier)
        except Exception:
            self.driver = self.session.refresh_after_activity(self.driver, 0)
            self.driver.activate_app(self.config.app_identifier)
        return self._wait_for_home(self.driver)

    def _wait_for_home(self, driver: Any) -> Any:
        """Wait for an actual app screen without assuming every case starts at home."""
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            try:
                source = str(driver.page_source or "")
                if source.strip():
                    return driver
            except Exception:
                pass
            time.sleep(0.25)
        raise RuntimeError("APP 启动后未加载出可读取页面，已阻止本轮用例执行")

    def refresh(self, driver: Any, settle_seconds: float) -> Any:
        self.driver = self.session.refresh_after_activity(driver, settle_seconds)
        return self.driver

    def close(self) -> None:
        if self.driver is None:
            return
        try:
            self.driver.quit()
        except Exception:
            pass
        finally:
            self.driver = None


def _requires_visual_oracle(plan: ExecutionPlan) -> bool:
    groups = [plan.oracles, plan.state_contract.restore_oracles]
    for requirement in plan.state_contract.requires:
        groups.extend((requirement.check_oracles, requirement.prepare_oracles))
    return any(oracle.type == OracleType.VISUAL_ASSERTION for group in groups for oracle in group)


def run_plan(
    plan: ExecutionPlan,
    run_dir: Path,
    platform: DevicePlatform | str | None = None,
    *,
    runtime_session: AppiumRuntimeSession | None = None,
) -> dict[str, Any]:
    plan, test_parameters = resolve_test_parameters(plan, run_id=run_dir.name)
    run_dir.mkdir(parents=True, exist_ok=True)
    owns_session = runtime_session is None
    runtime_session = runtime_session or AppiumRuntimeSession(platform)
    selected_platform = runtime_session.platform
    config = runtime_session.config
    if isinstance(config, AndroidDeviceConfig):
        adb = Path(os.getenv("ADB_PATH", str(DEFAULT_ADB)))
        evidence: AdbEvidenceCollector | IOSAppiumEvidenceCollector = AdbEvidenceCollector(
            run_dir, adb=adb, udid=config.udid
        )
    else:
        evidence = IOSAppiumEvidenceCollector(run_dir)
    visual_repair = VisualRepairService.from_environment()
    missing_visual_capability = visual_repair is None and _requires_visual_oracle(plan)
    started_at = datetime.now(timezone.utc).isoformat()
    driver = None
    executor = None
    execution = None
    status = "failed"
    classification = "AUTOMATION_FAILURE"
    message = "执行未完成"
    final_page_source = ""
    device_log = ""
    state_context: dict[str, Any] = {"mode": plan.state_contract.mode.value, "requirements": []}
    state_restore_error: str | None = None
    main_execution_started = False

    if not missing_visual_capability:
        try:
            evidence.clear_logcat()
        except Exception as exc:
            evidence.events.append({"type": "evidence_warning", "message": f"清理 logcat 失败：{exc}"})
    try:
        if missing_visual_capability:
            raise RuntimeError("视觉验收模型未配置；已在接触设备和修改数据前停止")
        driver = runtime_session.prepare()
        evidence.set_driver(driver)

        def refresh_driver(current_driver: Any, settle_seconds: float) -> Any:
            refreshed = runtime_session.refresh(current_driver, settle_seconds)
            evidence.set_driver(refreshed)
            return refreshed

        executor = AppiumPlanExecutor(
            driver,
            package=config.app_identifier,
            platform=selected_platform,
            event_sink=evidence.record,
            coordinate_tapper=(
                getattr(evidence, "tap_coordinates", None)
                if selected_platform == DevicePlatform.ANDROID else None
            ),
            driver_refresher=(refresh_driver if selected_platform == DevicePlatform.ANDROID else None),
            repair_handler=(
                _build_visual_repair_handler(
                    visual_repair,
                    evidence,
                    platform=selected_platform,
                    driver_refresher=(refresh_driver if selected_platform == DevicePlatform.ANDROID else None),
                )
                if visual_repair is not None
                else None
            ),
            visual_oracle_handler=(
                _build_visual_oracle_handler(visual_repair, evidence)
                if visual_repair is not None
                else None
            ),
            visual_wait_handler=(
                lambda step, current_driver: _build_visual_oracle_handler(visual_repair, evidence)(
                    Oracle(oracle_id=f"wait-{step.step_id}", title=step.title,
                           type="visual_assertion",
                           expected=str(step.metadata.get("visual_hint") or step.title)),
                    current_driver,
                )
                if visual_repair is not None
                else None
            ),
            widget_visual_matcher=(
                _build_widget_visual_matcher(visual_repair, evidence)
                if visual_repair is not None and selected_platform == DevicePlatform.ANDROID
                else None
            ),
            locator_snapshot=plan.locator_snapshot,
        )
        audit_locators = getattr(executor, "audit_locators", None)
        if callable(audit_locators):
            audit_locators(plan)
        if plan.state_contract.mode != StateMode.LEGACY_UNMANAGED:
            state_context = _prepare_state(plan, executor, evidence)
        main_execution_started = True
        execution = executor.execute(plan, phase="execution")
        driver = executor.driver
        runtime_session.driver = driver
        status = "passed"
        if any(step.ai_repair_used for step in execution.steps):
            classification = "AI_REPAIR_SUCCESS"
            message = "常规定位失败后由 AI 视觉修复，原硬验收标准通过"
        elif any(event.get("type") == "visual_oracle_result" for event in evidence.events):
            classification = "AI_VISUAL_ORACLE_PASS"
            message = "页面与数据硬验收通过，UI 树不可读的画面内容由 AI 视觉验收通过"
        elif any(
            event.get("type") == "driver_refresh" and event.get("reason") == "proxy_timeout_recovery"
            for event in evidence.events
        ):
            classification = "ENVIRONMENT_RECOVERED_PASS"
            message = "Appium 设备会话异常后自动恢复，测试步骤与硬验收标准通过"
        else:
            classification = "NORMAL_PASS"
            message = "测试步骤与硬验收标准全部通过"
    except StatePreparationError as exc:
        cause = exc.__cause__
        if cause is not None and _is_environment_error(cause):
            classification = "ENVIRONMENT_EXCEPTION"
        elif cause is not None and not isinstance(cause, OracleFailure):
            classification = "AUTOMATION_FAILURE"
        elif cause is not None and _is_oracle_automation_failure(cause):
            classification = "AUTOMATION_FAILURE"
        else:
            classification = "TEST_DATA_FAILURE"
        message = str(exc)
    except OracleFailure as exc:
        execution = exc.result
        classification, message = _classify_oracle_failure(exc, evidence.events)
    except PlanExecutionError as exc:
        execution = exc.result
        message = str(exc)
        if _is_environment_error(exc):
            classification = "ENVIRONMENT_EXCEPTION"
        elif exc.repair_eligible:
            classification = "AUTOMATION_FAILURE_REPAIRABLE"
        else:
            classification = "AUTOMATION_FAILURE"
    except Exception as exc:
        classification = "ENVIRONMENT_EXCEPTION"
        message = f"{type(exc).__name__}: {exc}"
    finally:
        state_change_possible = main_execution_started or any(
            event.get("type") == "state_prepare_started" for event in evidence.events
        )
        if executor is not None and state_change_possible and (
            plan.state_contract.restore.value == "always"
            or (plan.state_contract.restore.value == "on_success" and status == "passed")
        ):
            try:
                _restore_state(plan, executor, evidence)
            except Exception as exc:
                state_restore_error = f"{type(exc).__name__}: {exc}"
                evidence.events.append({"type": "state_restore_failed", "message": state_restore_error})
        try:
            final_page_source = evidence.capture_page_source("final-state.xml")
        except Exception as exc:
            evidence.events.append({"type": "evidence_warning", "message": f"最终 UI XML 采集失败：{exc}"})
        try:
            device_log = evidence.save_logcat()
        except Exception as exc:
            evidence.events.append({"type": "evidence_warning", "message": f"设备日志采集失败：{exc}"})
        active_driver = executor.driver if executor is not None else driver
        if active_driver is not None:
            runtime_session.driver = active_driver
        if owns_session:
            try:
                runtime_session.close()
            except Exception as exc:
                evidence.events.append({"type": "cleanup_warning", "message": str(exc)})

    if state_restore_error:
        if status == "passed":
            message = f"测试完成但状态恢复失败：{state_restore_error}"
        else:
            message = f"原执行结果：{message}；同时状态恢复失败：{state_restore_error}"
        status = "failed"
        classification = "TEST_DATA_FAILURE"
    result = {
        "schema_version": "1.0",
        "plan_id": plan.plan_id,
        "case_id": plan.source.case_id,
        "case_title": plan.source.title,
        "status": status,
        "classification": classification,
        "classification_label": _classification_label(classification),
        "message": message,
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "device": {
            "name": config.device_name,
            "udid": config.udid,
            "platform": "iOS" if selected_platform == DevicePlatform.IOS else "Android",
        },
        "steps": [item.__dict__ for item in execution.steps] if execution else [],
        "oracles": execution.oracles if execution else [],
        "events": evidence.events,
        "final_page_source": final_page_source,
        "device_log": device_log,
        "logcat": device_log,
        "compiler": {"mode": plan.compiler_mode, "model": plan.compiler_model},
        "qa": {"confirmed_by": plan.qa_confirmed_by, "confirmed_at": plan.qa_confirmed_at},
        "state_context": state_context,
        "state_restore_error": state_restore_error,
        "test_parameters": test_parameters,
    }
    resolved_plan_path = run_dir / "resolved-plan.json"
    resolved_plan_path.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    result["resolved_plan_path"] = str(resolved_plan_path.resolve())
    result_path = run_dir / "result.json"
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    result["result_path"] = str(result_path.resolve())
    result["report_status"] = "queued"
    result["report_url"] = f"/runs/{run_dir.name}/report.html"
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def resolve_test_parameters(
    plan: ExecutionPlan, *, today: date | None = None, run_id: str | None = None
) -> tuple[ExecutionPlan, list[dict[str, Any]]]:
    """Resolve explicitly declared run parameters and substitute ${name} references."""
    if not plan.test_parameters:
        return plan, []
    run_date = today or date.today()
    values: dict[str, str] = {}
    records = []
    for name, rule in sorted(plan.test_parameters.items()):
        if rule.source == "fixed":
            value = str(rule.value)
        elif rule.source == "today":
            value = (run_date + timedelta(days=rule.offset_days)).strftime(rule.date_format)
        else:
            if not run_id:
                raise ValueError("运行专属测试数据名称需要本次 run_id")
            value = f"{rule.value or plan.source.case_id}-{run_id}"
        values[name] = value
        records.append(
            {
                "name": name,
                "value": value,
                "source": rule.source,
                "offset_days": rule.offset_days if rule.source == "today" else None,
            }
        )

    def substitute(value: Any) -> Any:
        if isinstance(value, str):
            for name, actual in values.items():
                value = value.replace("${" + name + "}", actual)
            return value
        if isinstance(value, list):
            return [substitute(item) for item in value]
        if isinstance(value, dict):
            return {key: substitute(item) for key, item in value.items()}
        return value

    resolved = ExecutionPlan.model_validate(substitute(plan.model_dump(mode="json")))
    return resolved, records


def _fragment_plan(plan: ExecutionPlan, suffix: str, steps: list[PlanStep], oracles: list[Oracle]) -> ExecutionPlan:
    if not steps:
        steps = [PlanStep(step_id=f"{suffix}-launch", title="启动 APP", action="launch_app")]
    if not oracles:
        raise ValueError(f"状态片段 {suffix} 缺少明确核验项，不能用默认页面条件代替业务验收")
    return plan.model_copy(
        update={
            "plan_id": f"{plan.plan_id}::{suffix}",
            "steps": steps,
            "oracles": oracles,
        }
    )


def _run_state_fragment(plan: ExecutionPlan, executor: AppiumPlanExecutor, suffix: str, steps: list[PlanStep], oracles: list[Oracle]) -> None:
    fragment = _fragment_plan(plan, suffix, steps, oracles)
    if suffix.endswith("-check"):
        phase = "precondition_check"
    elif suffix.endswith("-prepare"):
        phase = "precondition_prepare"
    elif suffix.endswith("-verify"):
        phase = "precondition_verify"
    elif suffix == "state-restore":
        phase = "restoration"
    else:
        phase = "precondition"
    executor.execute(fragment, phase=phase)


def _prepare_state(plan: ExecutionPlan, executor: AppiumPlanExecutor, evidence: Any) -> dict[str, Any]:
    context: dict[str, Any] = {"mode": plan.state_contract.mode.value, "requirements": []}
    for index, requirement in enumerate(plan.state_contract.requires):
        prefix = f"state-{index}-{_safe_name(requirement.domain)}"
        try:
            _run_state_fragment(plan, executor, f"{prefix}-check", requirement.check_steps, requirement.check_oracles)
            context["requirements"].append({"domain": requirement.domain, "status": "verified", "prepared": False})
            evidence.record({"type": "state_check_passed", "domain": requirement.domain, "phase": "precondition_check"})
            continue
        except Exception as check_error:
            if (
                not isinstance(check_error, OracleFailure)
                or _is_environment_error(check_error)
                or _is_oracle_automation_failure(check_error)
            ):
                evidence.record({"type": "state_check_failed", "domain": requirement.domain, "message": str(check_error), "phase": "precondition_check"})
                raise StatePreparationError(
                    f"前置检查无法可靠判断数据状态：{requirement.description}"
                ) from check_error
            if not requirement.prepare_steps:
                evidence.record({"type": "state_check_failed", "domain": requirement.domain, "message": str(check_error), "phase": "precondition_check"})
                raise StatePreparationError(f"前置条件未满足且没有可用准备流程：{requirement.description}") from check_error
            try:
                evidence.record({"type": "state_prepare_started", "domain": requirement.domain, "phase": "precondition_prepare"})
                _run_state_fragment(plan, executor, f"{prefix}-prepare", requirement.prepare_steps, requirement.prepare_oracles)
                _run_state_fragment(plan, executor, f"{prefix}-verify", requirement.check_steps, requirement.check_oracles)
            except Exception as prepare_error:
                evidence.record({"type": "state_prepare_failed", "domain": requirement.domain, "message": str(prepare_error), "phase": "precondition_prepare"})
                raise StatePreparationError(f"前置条件准备失败：{requirement.description}") from prepare_error
            context["requirements"].append({"domain": requirement.domain, "status": "verified", "prepared": True})
            evidence.record({"type": "state_prepare_passed", "domain": requirement.domain, "phase": "precondition_prepare"})
    return context


def _restore_state(plan: ExecutionPlan, executor: AppiumPlanExecutor, evidence: Any) -> None:
    if not plan.state_contract.restore_steps:
        return
    restore_steps = plan.state_contract.restore_steps
    if plan.state_contract.mode.value == "destructive":
        created_events = [event for event in evidence.events
                          if event.get("type") == "state_object_created" and event.get("identity")]
        if any(event.get("object_key") for event in created_events) and any(
            not event.get("object_key") for event in created_events
        ):
            raise RuntimeError("本轮创建记录混用对象键与旧名称，不能安全恢复")
        created_with_keys = {
            str(event["object_key"]): str(event["identity"])
            for event in created_events if event.get("object_key")
        }
        if created_with_keys:
            verified_keys = {
                str(event["object_key"])
                for event in evidence.events
                if event.get("type") == "state_object_verified"
                and event.get("identity") == created_with_keys.get(str(event.get("object_key")))
            }
            if verified_keys != set(created_with_keys):
                raise RuntimeError("本轮创建对象尚未核实唯一身份，拒绝删除")
            selected_steps = [step for step in restore_steps
                              if step.metadata.get("owns_object_key") in created_with_keys]
            selected_keys = {str(step.metadata.get("owns_object_key")) for step in selected_steps}
            if selected_keys != set(created_with_keys):
                raise RuntimeError("缺少本轮对象键对应的精确恢复步骤，拒绝宽泛清理")
            for step in selected_steps:
                key = str(step.metadata["owns_object_key"])
                identity = created_with_keys[key]
                target_key = step.metadata.get("owned_object_target_key")
                if (step.metadata.get("owns_object_identity") != identity
                        or step.metadata.get("owned_object_expected_text") != identity
                        or not target_key):
                    raise RuntimeError(f"恢复前对象键 {key} 与名称不一致，拒绝清理")
                elements = executor._find_all(None, str(target_key))
                if len(elements) != 1 or str(getattr(elements[0], "text", "") or "") != identity:
                    raise RuntimeError(f"恢复前对象键 {key} 的目标不唯一或名称不匹配，拒绝清理")
            restore_steps = selected_steps
        else:
            created = {
                str(event["identity"]) for event in created_events
            }
            if not created:
                evidence.record({
                    "type": "state_restore_failed",
                    "reason": "破坏性执行没有记录本轮创建对象的身份，拒绝宽泛清理",
                    "phase": "restoration",
                })
                raise RuntimeError("破坏性执行未记录本轮对象身份，无法安全恢复")
            selected_steps = [
                step for step in restore_steps
                if step.metadata.get("owns_object_identity") in created
            ]
            selected_identities = {str(step.metadata.get("owns_object_identity")) for step in selected_steps}
            if selected_identities != created:
                missing = sorted(created - selected_identities)
                raise RuntimeError(f"缺少本轮对象的精确恢复步骤，拒绝宽泛清理：{missing}")
            verified: set[str] = set()
            for step in selected_steps:
                identity = str(step.metadata.get("owns_object_identity"))
                target_key = step.metadata.get("owned_object_target_key")
                if identity in verified:
                    continue
                if not target_key or step.metadata.get("owned_object_expected_text") != identity:
                    continue
                elements = executor._find_all(None, str(target_key))
                if len(elements) != 1:
                    raise RuntimeError(f"恢复前对象身份不唯一，拒绝清理：{identity}")
                element = elements[0]
                observed = str(getattr(element, "text", "") or "")
                if not observed and hasattr(element, "get_attribute"):
                    observed = str(element.get_attribute("content-desc") or "")
                if observed != identity:
                    raise RuntimeError(f"恢复前对象身份不匹配，拒绝清理：期望 {identity}，实际 {observed}")
                verified.add(identity)
            if verified != created:
                missing = sorted(created - verified)
                raise RuntimeError(f"恢复步骤未声明可核对的对象身份，拒绝清理：{missing}")
            restore_steps = selected_steps
    _run_state_fragment(
        plan,
        executor,
        "state-restore",
        restore_steps,
        plan.state_contract.restore_oracles,
    )
    evidence.record({"type": "state_restore_passed", "domains": plan.state_contract.mutates, "phase": "restoration"})


def render_report(plan: ExecutionPlan, result: dict[str, Any], run_dir: Path) -> Path:
    environment = Environment(
        loader=FileSystemLoader(BASE_DIR / "templates"),
        autoescape=select_autoescape(["html"]),
    )
    template = environment.get_template("report.html")
    target = run_dir / "report.html"
    target.write_text(template.render(plan=plan, result=result), encoding="utf-8")
    return target


def _is_environment_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in (
        "could not proxy", "connection refused", "invalid session", "session not created",
        "adb device offline", "device unauthorized",
    )) or ("timeout" in message and any(term in message for term in ("proxy", "connection", "session", "command")))


def _is_oracle_automation_failure(exc: Exception) -> bool:
    message = str(exc)
    markers = (
        "未找到或无法读取验收目标",
        "读取文本顺序失败",
        "视觉验收模型未配置",
    )
    return any(marker in message for marker in markers)


def _classify_oracle_failure(exc: OracleFailure, events: list[dict[str, Any]]) -> tuple[str, str]:
    """Record the observed mismatch without claiming a product root cause."""
    if any(event.get("type") == "ai_repair_succeeded" for event in events):
        return (
            "AUTOMATION_FAILURE",
            f"AI 视觉修复执行后硬验收未通过，不能归因于产品：{exc}",
        )
    visual_results = [event for event in events if event.get("type") == "visual_oracle_result"]
    if visual_results and not visual_results[-1].get("conclusive", False):
        return "AUTOMATION_FAILURE", f"视觉验收证据不足：{exc}"
    if _is_oracle_automation_failure(exc):
        return "AUTOMATION_FAILURE", f"验收器无法读取目标：{exc}"
    return "ACCEPTANCE_MISMATCH", str(exc)


def _build_widget_visual_matcher(
    service: VisualRepairService,
    evidence: AdbEvidenceCollector | IOSAppiumEvidenceCollector,
):
    """Identify canvas-rendered launcher widgets by text, then bind the point to a real host view."""
    visible_markers = {
        "luggage": ("行李已整理", "行李清单"),
        "reminder": ("出行提醒", "行程提醒"),
        "itinerary": ("今日行程",),
        "bill": ("旅行账单", "总花费", "总预算", "已超支"),
        "placeholder": ("点击选择", "心仪的组件"),
    }

    def match(step: PlanStep, widgets: list[Any], driver: Any):
        widget_type = str(step.metadata.get("widget_type") or "").lower()
        markers = visible_markers.get(widget_type)
        if not markers:
            return None
        filename = f"widget-identity-{_safe_name(step.step_id)}-{len(evidence.events):04d}.png"
        screenshot = evidence.capture_screenshot(filename)
        image_path = evidence.run_dir / screenshot
        requirement = (
            f"屏幕上是否有 app1 桌面组件，卡片内清楚显示以下文字之一：{'、'.join(markers)}？"
            "只判断可见卡片内的实际文字。"
            + ("目标必须是未绑定内容的空白占位卡片。" if widget_type == "placeholder"
               else "写着‘点击选择心仪的组件吧’的空白卡片不计。")
        )
        try:
            assertion = service.verify(image_path, requirement=requirement)
        except Exception as exc:
            evidence.record({"type": "visual_widget_identity_inconclusive", "step_id": step.step_id,
                             "message": f"{type(exc).__name__}: {exc}", "screenshot": screenshot})
            return None
        if (
            not assertion.passed
            or assertion.confidence < service.minimum_confidence
            or not any(marker in assertion.observed_text for marker in markers)
        ):
            return None
        proposal_step = step.model_copy(deep=True)
        proposal_step.metadata["visual_hint"] = (
            f"只定位卡片内清楚显示{'或'.join(markers)}的 app1 桌面组件，"
            "在该卡片内部取一点；"
            + ("必须选择空白占位卡片。" if widget_type == "placeholder" else "不能选择空白卡片。")
            + "找不到则 visible=false。"
        )
        try:
            proposal = service.propose(proposal_step, image_path)
        except Exception as exc:
            evidence.record({"type": "visual_widget_identity_inconclusive", "step_id": step.step_id,
                             "message": f"{type(exc).__name__}: {exc}", "screenshot": screenshot})
            return None
        if not service.accepted(proposal):
            return None
        width, height = evidence.screen_size()
        point_x, point_y = proposal.x * width, proposal.y * height
        matching_hosts = []
        for widget in widgets:
            rect = widget.rect
            if (rect["x"] <= point_x <= rect["x"] + rect["width"]
                    and rect["y"] <= point_y <= rect["y"] + rect["height"]):
                matching_hosts.append(widget)
        if len(matching_hosts) != 1:
            return None
        evidence.record({
            "type": "visual_widget_identity_confirmed",
            "step_id": step.step_id,
            "widget_type": widget_type,
            "observed_text": assertion.observed_text,
            "confidence": min(assertion.confidence, proposal.confidence),
            "widget_bounds": dict(matching_hosts[0].rect),
            "pixel_x": point_x,
            "pixel_y": point_y,
            "screenshot": screenshot,
        })
        return matching_hosts[0]

    return match


def _build_visual_repair_handler(
    service: VisualRepairService,
    evidence: AdbEvidenceCollector | IOSAppiumEvidenceCollector,
    *,
    platform: DevicePlatform | str = DevicePlatform.ANDROID,
    driver_refresher=None,
):
    selected_platform = DevicePlatform(platform)

    def repair(step, driver, error) -> RepairOutcome:
        if step.action not in {ActionType.TAP, ActionType.INPUT_TEXT} or (
            step.action == ActionType.TAP and step.metadata.get("tap_count", 1) != 1
        ):
            return RepairOutcome(False, "视觉补救仅支持单次点击和可核对结果的文字输入")
        filename = f"ai-repair-{_safe_name(step.step_id)}.png"
        relative_screenshot = evidence.capture_screenshot(filename)
        proposal = service.propose(step, evidence.run_dir / relative_screenshot)
        proposal_data = proposal.model_dump()
        proposal_data["screenshot"] = relative_screenshot
        if not service.accepted(proposal):
            return RepairOutcome(
                False,
                f"视觉候选未通过阈值：visible={proposal.visible}, confidence={proposal.confidence}",
                proposal_data,
            )
        width, height = evidence.screen_size()
        x = round(proposal.x * width)
        y = round(proposal.y * height)
        repaired_driver = None
        gesture = "mobile: tap" if selected_platform == DevicePlatform.IOS else "mobile: clickGesture"
        try:
            driver.execute_script(gesture, {"x": x, "y": y})
        except Exception as exc:
            if not (_is_environment_error(exc) and driver_refresher):
                raise
            evidence.record(
                {
                    "type": "driver_refresh",
                    "reason": "ai_repair_click_outcome_uncertain",
                    "message": str(exc),
                }
            )
            repaired_driver = driver_refresher(driver, 0)
            return RepairOutcome(False, "视觉点击响应超时，结果无法确认，已停止重复点击",
                                 proposal_data, driver=repaired_driver)
        if step.action == ActionType.INPUT_TEXT:
            field = driver.switch_to.active_element
            focused = field.get_attribute("focused")
            if str(focused).lower() == "false":
                return RepairOutcome(False, "视觉定位后输入框未获得焦点", proposal_data)
            field.clear()
            field.send_keys(step.value)
            actual = str(field.get_attribute("text") or getattr(field, "text", "") or "")
            if actual != step.value:
                return RepairOutcome(False, "视觉输入后无法确认内容与计划一致", proposal_data)
        else:
            time.sleep(2)
        proposal_data.update({"pixel_x": x, "pixel_y": y, "original_error": str(error)})
        return RepairOutcome(
            True,
            f"AI 视觉已完成{('文字输入并核对' if step.action == ActionType.INPUT_TEXT else '单次点击')}"
            f"（置信度 {proposal.confidence:.2f}），等待原验收规则复核",
            proposal_data,
            driver=repaired_driver,
        )

    return repair


def _build_visual_oracle_handler(
    service: VisualRepairService,
    evidence: AdbEvidenceCollector | IOSAppiumEvidenceCollector,
):
    def verify(oracle, driver):
        stem = f"visual-oracle-{_safe_name(oracle.oracle_id)}-{len(getattr(evidence, 'events', [])):04d}"
        checkpoint = _capture_checkpoint(evidence, stem)
        relative_screenshot = checkpoint.get("screenshot")
        if not relative_screenshot:
            raise RuntimeError(checkpoint.get("evidence_warning", "视觉验收截图未采集"))
        history = select_screenshots(evidence.run_dir, getattr(evidence, "events", []), limit=10)
        assertion = service.verify(
            evidence.run_dir / relative_screenshot,
            requirement=str(oracle.expected),
            **({"history": history} if history else {}),
        )
        conclusive = service.assertion_is_conclusive(assertion)
        passed = bool(assertion.passed and conclusive)
        message = (
            f"视觉判断：{'通过' if assertion.passed else '不通过'}；"
            f"置信度 {assertion.confidence:.2f}；实际观察：{assertion.observed_text or '未提取到文字'}；"
            f"依据：{assertion.reason}"
        )
        return passed, message, {
            **checkpoint,
            "confidence": assertion.confidence,
            "observed_text": assertion.observed_text,
            "reason": assertion.reason,
            "conclusive": conclusive,
            "screenshot": relative_screenshot,
            "history_screenshots": [event for _, event in history],
        }

    return verify


def _safe_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-") or "evidence"


def _classification_label(classification: str) -> str:
    labels = {
        "NORMAL_PASS": "正常通过",
        "AI_REPAIR_SUCCESS": "AI 视觉修复后通过",
        "AI_VISUAL_ORACLE_PASS": "AI 视觉验收后通过",
        "ENVIRONMENT_RECOVERED_PASS": "环境恢复后通过",
        "ACCEPTANCE_MISMATCH": "验收不一致（待 QA 确认）",
        "PRODUCT_FAILURE": "历史验收不一致（待 QA 确认）",
        "AUTOMATION_FAILURE_REPAIRABLE": "自动化定位失败（可修复）",
        "AUTOMATION_FAILURE": "自动化执行失败",
        "ENVIRONMENT_EXCEPTION": "设备或环境异常",
    }
    return labels.get(classification, classification)
