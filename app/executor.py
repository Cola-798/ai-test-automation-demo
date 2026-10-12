from __future__ import annotations

import json
import os
import re
import time
from xml.etree import ElementTree as ET
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from appium.webdriver.common.appiumby import AppiumBy
from selenium.common.exceptions import StaleElementReferenceException

from .locator_catalog import post_tap_page_marker, resolve_target, saved_widget_locator
from .schemas import (
    ActionType,
    DevicePlatform,
    ExecutionPlan,
    Locator,
    LocatorStrategy,
    Oracle,
    OracleType,
    PlanStep,
)


class PlanExecutionError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        step: PlanStep | None = None,
        repair_eligible: bool = False,
        result: "ExecutionResult | None" = None,
    ):
        super().__init__(message)
        self.step = step
        self.repair_eligible = repair_eligible
        self.result = result


class OracleFailure(AssertionError):
    def __init__(self, message: str, *, oracle: Oracle, result: "ExecutionResult"):
        super().__init__(message)
        self.oracle = oracle
        self.result = result


class AmbiguousLocatorError(LookupError):
    """A locator matched multiple visible candidates without an identity rule."""


class TargetNotFoundError(LookupError):
    """No candidate identified a target before its action started."""


class PostconditionFailure(LookupError):
    """An action was dispatched but its required result was not observed."""


class SequencePrefixMismatch(LookupError):
    def __init__(self, observed: list[str]):
        self.observed = observed
        super().__init__("列表起始顺序与期望不符")


def _can_visual_repair(step: PlanStep, error: Exception) -> bool:
    return bool(
        step.allow_ai_repair
        and isinstance(error, TargetNotFoundError)
        and step.action in {ActionType.TAP, ActionType.INPUT_TEXT}
        and (step.locator or step.target_key)
        and not step.metadata.get("plan_row_tap")
        and not step.metadata.get("saved_widget_match")
        and not step.metadata.get("choose_different_color")
        and not step.metadata.get("coordinate_tap")
        and (step.action != ActionType.TAP or step.metadata.get("tap_count", 1) == 1)
    )


def _record_created_state_object(
    step: PlanStep, event_sink: Callable[[dict[str, Any]], None], context: dict[str, Any]
) -> None:
    identity = step.metadata.get("creates_object_identity")
    if isinstance(identity, str) and identity.strip():
        event_sink({"type": "state_object_created", "identity": identity,
                    "object_key": step.metadata.get("created_object_key"),
                    "step_id": step.step_id, **context})


@dataclass
class RepairOutcome:
    success: bool
    message: str
    proposal: dict[str, Any] = field(default_factory=dict)
    driver: Any | None = None


@dataclass
class StepResult:
    step_id: str
    title: str
    status: str
    duration_seconds: float
    locator_strategy: str | None = None
    message: str = ""
    ai_repair_used: bool = False
    phase: str = "execution"
    step_index: int = 0


@dataclass
class ExecutionResult:
    plan_id: str
    status: str
    steps: list[StepResult] = field(default_factory=list)
    oracles: list[dict[str, Any]] = field(default_factory=list)


class AppiumPlanExecutor:
    def __init__(
        self,
        driver: Any,
        *,
        package: str,
        platform: DevicePlatform | str = DevicePlatform.ANDROID,
        event_sink: Callable[[dict], None] | None = None,
        settle_seconds: float = 2.0,
        driver_refresher: Callable[[Any, float], Any] | None = None,
        repair_handler: Callable[[PlanStep, Any, Exception], RepairOutcome] | None = None,
        visual_oracle_handler: Callable[[Oracle, Any], tuple[bool, str, dict[str, Any]]] | None = None,
        visual_wait_handler: Callable[[PlanStep, Any], tuple[bool, str, dict[str, Any]]] | None = None,
        widget_visual_matcher: Callable[[PlanStep, list[Any], Any], Any | None] | None = None,
        locator_snapshot: dict[str, list[Locator]] | None = None,
        coordinate_tapper: Callable[[int, int], None] | None = None,
    ):
        self.driver = driver
        self.package = package
        self.platform = DevicePlatform(platform)
        self.event_sink = event_sink or (lambda event: None)
        self.settle_seconds = settle_seconds
        self.driver_refresher = driver_refresher
        self.repair_handler = repair_handler
        self.visual_oracle_handler = visual_oracle_handler
        self.visual_wait_handler = visual_wait_handler
        self.widget_visual_matcher = widget_visual_matcher
        self.locator_snapshot = locator_snapshot or {}
        self.coordinate_tapper = coordinate_tapper
        self._captured_element_counts: dict[str, int] = {}
        self._captured_visible_counts: set[str] = set()
        self._created_objects: dict[str, dict[str, Any]] = {}
        self._captured_text: dict[str, str] = {}

    def execute(self, plan: ExecutionPlan, *, phase: str = "execution") -> ExecutionResult:
        result = ExecutionResult(plan_id=plan.plan_id, status="running")
        for step in plan.steps:
            if step.metadata.get("capture_element_count"):
                self._captured_element_counts.pop(step.step_id, None)
                self._captured_visible_counts.discard(step.step_id)
        for step_index, step in enumerate(plan.steps, start=1):
            step = self._resolve_captured_text(step)
            context = {"phase": phase, "step_index": step_index}
            self.event_sink({"type": "step_started", "step_id": step.step_id, "title": step.title, **context})
            started = time.monotonic()
            action_completed = False
            try:
                self._check_owned_object(step)
                self._execute_step(step)
                action_completed = True
                if isinstance(step.metadata.get("postcondition"), dict) and (
                    step.action != ActionType.TAP or step.metadata.get("choose_different_color")
                ):
                    self._wait_after_tap(step, None)
                if step.metadata.get("capture_text_as"):
                    name = str(step.metadata["capture_text_as"])
                    element = self._find(step.locator, step.target_key, timeout_seconds=step.timeout_seconds)
                    value = self._element_text(element)
                    if not value or value.startswith("请选择"):
                        raise LookupError("尚未选中实际测试数据，不能把占位文字当对象身份")
                    self._captured_text[name] = value
                    self.event_sink({"type": "test_object_observed", "name": name, "value": value,
                                     "step_id": step.step_id, **context})
                self._verify_created_object(step, context)
                capture_target = step.metadata.get("capture_element_count")
                if capture_target:
                    if not isinstance(capture_target, dict):
                        raise ValueError("capture_element_count 必须包含 locator 或 target_key")
                    locator_data = capture_target.get("locator")
                    if locator_data is None and "strategy" in capture_target:
                        locator_data = capture_target
                    locator = Locator.model_validate(locator_data) if locator_data else None
                    target_key = capture_target.get("target_key")
                    if locator is None and not target_key:
                        raise ValueError("capture_element_count 必须包含 locator 或 target_key")
                    visible_only = capture_target.get("visible_only") is True
                    count = len(self._saved_widget_rows(capture_target["saved_widget_match"])) if capture_target.get("saved_widget_match") else (
                        len(self._find_all(locator, target_key)) if visible_only
                        else len(self._collect_text_sequence(
                            locator, target_key, None, must_reach_end=True, include_empty=True,
                        ))
                    )
                    self._captured_element_counts[step.step_id] = count
                    if visible_only and not capture_target.get("saved_widget_match"):
                        self._captured_visible_counts.add(step.step_id)
                    self.event_sink({"type": "element_count_captured", "step_id": step.step_id, "count": count,
                                     "scope": "visible_viewport" if visible_only else "full_list",
                                     "phase": phase, "step_index": step_index})
            except Exception as exc:
                duration = round(time.monotonic() - started, 3)
                if (
                    self.visual_wait_handler
                    and not action_completed
                    and step.action in {ActionType.WAIT_FOR, ActionType.ASSERT_VISIBLE, ActionType.ASSERT_PAGE}
                    and not step.metadata.get("wait_until_gone")
                    and not _is_environment_error(exc)
                    and not step.metadata.get("widget_type")
                    and not step.metadata.get("saved_widget_match")
                ):
                    try:
                        passed, message, details = self.visual_wait_handler(step, self.driver)
                    except Exception as visual_error:
                        passed, message, details = False, f"{type(visual_error).__name__}: {visual_error}", {}
                    if passed:
                        result.steps.append(StepResult(
                            step_id=step.step_id, title=step.title, status="passed",
                            duration_seconds=duration, locator_strategy="ai_visual_check",
                            message=message, phase=phase, step_index=step_index,
                        ))
                        self.event_sink({"type": "visual_wait_confirmed", "step_id": step.step_id,
                                         "message": message, **details, **context})
                        continue
                    self.event_sink({"type": "visual_wait_inconclusive", "step_id": step.step_id,
                                     "message": message, **details, **context})
                repair_eligible = _can_visual_repair(step, exc)
                if repair_eligible and self.repair_handler:
                    self.event_sink({"type": "ai_repair_started", "step_id": step.step_id, "message": str(exc), **context})
                    try:
                        repair = self.repair_handler(step, self.driver, exc)
                    except Exception as repair_exc:
                        repair = RepairOutcome(False, f"{type(repair_exc).__name__}: {repair_exc}")
                    if repair.driver is not None:
                        self.driver = repair.driver
                    if repair.success and step.action == ActionType.TAP:
                        try:
                            self._wait_after_tap(step, step.target_key)
                        except Exception as verify_error:
                            repair = RepairOutcome(
                                False, f"视觉点击后未满足原步骤条件：{verify_error}",
                                repair.proposal, driver=repair.driver,
                            )
                    if repair.success:
                        result.steps.append(
                            StepResult(
                                step_id=step.step_id,
                                title=step.title,
                                status="passed",
                                duration_seconds=duration,
                                locator_strategy="ai_visual",
                                message=repair.message,
                                ai_repair_used=True,
                                phase=phase,
                                step_index=step_index,
                            )
                        )
                        self.event_sink(
                            {
                                "type": "ai_repair_succeeded",
                                "step_id": step.step_id,
                                "message": repair.message,
                                "proposal": repair.proposal,
                                "screenshot": repair.proposal.get("screenshot"),
                                **context,
                            }
                        )
                        self._record_created_object(step, context)
                        continue
                    self.event_sink(
                        {"type": "ai_repair_failed", "step_id": step.step_id, "message": repair.message, **context}
                    )
                result.steps.append(
                    StepResult(
                        step_id=step.step_id,
                        title=step.title,
                        status="failed",
                        duration_seconds=duration,
                        locator_strategy=step.locator.strategy if step.locator else ("target_key" if step.target_key else None),
                        message=f"{type(exc).__name__}: {exc}",
                        phase=phase,
                        step_index=step_index,
                    )
                )
                result.status = "failed"
                self.event_sink({"type": "step_failed", "step_id": step.step_id, "message": str(exc), **context})
                raise PlanExecutionError(
                    f"步骤 {step.step_id} 执行失败：{exc}",
                    step=step,
                    repair_eligible=repair_eligible,
                    result=result,
                ) from exc
            duration = round(time.monotonic() - started, 3)
            result.steps.append(
                StepResult(
                    step_id=step.step_id,
                    title=step.title,
                    status="passed",
                    duration_seconds=duration,
                    locator_strategy=step.locator.strategy if step.locator else ("target_key" if step.target_key else None),
                    phase=phase,
                    step_index=step_index,
                )
            )
            self.event_sink({"type": "step_passed", "step_id": step.step_id, "duration": duration, **context})
            self._record_created_object(step, context)

        for oracle_index, oracle in enumerate(plan.oracles, start=1):
            oracle = self._resolve_captured_text(oracle)
            passed, message = self._evaluate_oracle(oracle)
            result.oracles.append({"oracle_id": oracle.oracle_id, "passed": passed, "message": message})
            self.event_sink({
                "type": "oracle_result", "oracle_id": oracle.oracle_id,
                "oracle_index": oracle_index, "phase": "acceptance",
                "passed": passed, "message": message,
            })
            if not passed:
                result.status = "failed"
                raise OracleFailure(f"验收失败 {oracle.oracle_id}：{message}", oracle=oracle, result=result)
        result.status = "passed"
        return result

    def _resolve_captured_text(self, item):
        def replace(value):
            if isinstance(value, str):
                for name, text in self._captured_text.items():
                    value = value.replace("${" + name + "}", text)
                return value
            if isinstance(value, dict):
                return {key: replace(child) for key, child in value.items()}
            if isinstance(value, list):
                return [replace(child) for child in value]
            return value
        return type(item).model_validate(replace(item.model_dump())) if self._captured_text else item

    def _record_created_object(self, step: PlanStep, context: dict[str, Any]) -> None:
        _record_created_state_object(step, self.event_sink, context)
        identity = step.metadata.get("creates_object_identity")
        key = step.metadata.get("created_object_key")
        if isinstance(identity, str) and identity.strip() and isinstance(key, str) and key.strip():
            self._created_objects[key] = {"identity": identity.strip(), "verified": False,
                                          "step_id": step.step_id}

    def _verify_created_object(self, step: PlanStep, context: dict[str, Any]) -> None:
        key = step.metadata.get("verifies_created_object_key")
        if not key:
            return
        record = self._created_objects.get(str(key))
        if record is None:
            raise LookupError(f"本次运行没有创建对象键 {key}，不能核实其他运行的数据")
        matches = [element for element in self._find_all(step.locator, step.target_key)
                   if self._element_text(element) == record["identity"] and element.is_displayed()]
        if len(matches) != 1:
            raise LookupError(f"本次对象 {key} 的名称 {record['identity']} 应唯一可见，实际匹配 {len(matches)} 条")
        record["verified"] = True
        self.event_sink({"type": "state_object_verified", "object_key": key,
                         "identity": record["identity"], "step_id": step.step_id, **context})

    def _check_owned_object(self, step: PlanStep) -> None:
        key = step.metadata.get("owns_object_key")
        if not key:
            return
        record = self._created_objects.get(str(key))
        if record is None or not record["verified"]:
            raise LookupError(f"对象键 {key} 未在本次运行核实，不能编辑或删除")
        expected = step.metadata.get("owns_object_identity")
        if expected and expected != record["identity"]:
            raise LookupError(f"对象键 {key} 与目标名称不一致，不能编辑或删除")

    def _execute_step(self, step: PlanStep) -> None:
        if step.action == ActionType.WAIT_FOR and step.metadata.get("wait_until_gone") is True:
            deadline = time.monotonic() + step.timeout_seconds
            while True:
                try:
                    visible = any(element.is_displayed() for element in self._find_all(step.locator, step.target_key))
                except StaleElementReferenceException:
                    # Refresh the query; a stale reference alone does not prove exit.
                    visible = True
                if not visible:
                    self.event_sink({"type": "wait_target_gone", "step_id": step.step_id,
                                     "target_key": step.target_key})
                    return
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"等待页面退出超时，目标仍可见或刷新中：{step.target_key or step.locator.value}")
                time.sleep(min(0.2, max(0, deadline - time.monotonic())))
        if step.action == ActionType.LAUNCH_APP:
            if step.metadata.get("restart_app") is True:
                self.driver.terminate_app(self.package)
            self.driver.activate_app(self.package)
        elif step.action == ActionType.BACK:
            try:
                self.driver.back()
            except Exception as exc:
                if not self._recover_ambiguous_action(
                    step,
                    exc,
                    failure_reason="ambiguous_back_timeout",
                    verified_reason="ambiguous_back_timeout_verified",
                ):
                    raise
            time.sleep(self.settle_seconds)
        elif step.action == ActionType.BACKGROUND_APP:
            seconds = float(step.metadata.get("seconds", 2))
            self.driver.background_app(seconds)
            time.sleep(self.settle_seconds)
        elif step.action == ActionType.GO_HOME:
            if self.platform != DevicePlatform.ANDROID:
                raise RuntimeError("当前回到桌面步骤仅适用于 Android 真机")
            self.driver.press_keycode(3)
            time.sleep(self.settle_seconds)
        elif step.action == ActionType.LOCK_SCREEN:
            self.driver.lock(2)
            if not self.driver.is_locked():
                raise RuntimeError("锁屏动作后设备仍处于解锁状态")
        elif step.action == ActionType.UNLOCK_SCREEN:
            if self.platform != DevicePlatform.ANDROID:
                raise RuntimeError("当前解锁步骤仅适用于 Android 真机")
            pin = os.getenv("ANDROID_DEVICE_UNLOCK_PIN", "")
            if not pin or not pin.isdecimal():
                raise RuntimeError("缺少 Android 真机锁屏密码")
            self.driver.press_keycode(224)
            self.driver.swipe(520, 1850, 520, 500, 300)
            password_entry = self.driver.find_elements(AppiumBy.XPATH, "//*[@text='使用密码']")
            if password_entry:
                password_entry[0].click()
            for digit in pin:
                self.driver.find_element(
                    AppiumBy.XPATH,
                    f"//*[@resource-id='com.android.systemui:id/example_digit_text' and @text='{digit}']",
                ).click()
            deadline = time.monotonic() + step.timeout_seconds
            while self.driver.is_locked() and time.monotonic() < deadline:
                time.sleep(0.2)
            if self.driver.is_locked():
                raise RuntimeError("输入密码后设备仍处于锁屏状态")
        elif step.action == ActionType.TERMINATE_APP:
            self.driver.terminate_app(self.package)
            time.sleep(self.settle_seconds)
        elif step.action == ActionType.LONG_PRESS:
            if self.platform != DevicePlatform.ANDROID:
                raise RuntimeError("当前长按步骤仅适用于 Android 真机")
            duration_ms = int(step.metadata.get("duration_ms", 1200))
            if duration_ms <= 0:
                raise ValueError("长按时长必须大于零")
            element = self._find(step.locator, step.target_key, timeout_seconds=step.timeout_seconds)
            self.driver.execute_script(
                "mobile: longClickGesture",
                {"elementId": element.id, "duration": duration_ms},
            )
            time.sleep(self.settle_seconds)
        elif step.action == ActionType.SWIPE:
            values = step.metadata.get("coordinates", [0.5, 0.8, 0.5, 0.3])
            element_target_key = step.metadata.get("element_target_key")
            if element_target_key:
                slider = self._find(None, element_target_key, timeout_seconds=step.timeout_seconds)
                rect = slider.rect
                x1 = round(rect["x"] + values[0] * rect["width"])
                y1 = round(rect["y"] + values[1] * rect["height"])
                x2 = round(rect["x"] + values[2] * rect["width"])
                y2 = round(rect["y"] + values[3] * rect["height"])
            else:
                size = self._get_window_size_with_recovery(step)
                x1, y1, x2, y2 = [round(values[i] * (size["width"] if i % 2 == 0 else size["height"])) for i in range(4)]
            try:
                if step.metadata.get("driver_gesture") == "mobile_drag":
                    duration_ms = int(step.metadata.get("duration_ms", 650))
                    distance = max(abs(x2 - x1), abs(y2 - y1), 1)
                    speed = max(1, round(distance / max(duration_ms / 1000, 0.001)))
                    self.driver.execute_script(
                        "mobile: dragGesture",
                        {
                            "startX": x1,
                            "startY": y1,
                            "endX": x2,
                            "endY": y2,
                            "speed": speed,
                        },
                    )
                else:
                    self.driver.swipe(x1, y1, x2, y2, int(step.metadata.get("duration_ms", 650)))
            except Exception as exc:
                if not self._recover_ambiguous_swipe(step, exc):
                    raise
            time.sleep(self.settle_seconds)
        else:
            if step.metadata.get("choose_different_color"):
                if step.action != ActionType.TAP:
                    raise ValueError("颜色切换只支持点击动作")
                self._tap_different_color(step)
                return
            if step.locator and step.locator.strategy == LocatorStrategy.NORMALIZED_COORDINATE:
                if step.action != ActionType.TAP:
                    raise ValueError("已验证坐标补丁仅允许用于点击动作")
                x_text, y_text = step.locator.value.split(",", maxsplit=1)
                x_ratio = float(x_text)
                y_ratio = float(y_text)
                if not 0 <= x_ratio <= 1 or not 0 <= y_ratio <= 1:
                    raise ValueError("归一化坐标必须位于 0 到 1 之间")
                size = self._get_window_size_with_recovery(step)
                self.driver.execute_script(
                    "mobile: tap" if self.platform == DevicePlatform.IOS else "mobile: clickGesture",
                    {"x": round(x_ratio * size["width"]), "y": round(y_ratio * size["height"])},
                )
                self.event_sink(
                    {
                        "type": "repair_patch_reused",
                        "step_id": step.step_id,
                        "locator": step.locator.model_dump(mode="json"),
                    }
                )
                self._wait_after_tap(step, step.metadata.get("patched_target_key"))
                return
            if step.metadata.get("saved_widget_match"):
                element = self._find_saved_widget(step)
            elif (step.target_key == "launcher.app1_widget" or (
                step.locator is not None
                and step.locator.strategy == LocatorStrategy.ANDROID_UIAUTOMATOR
                and 'className("android.appwidget.AppWidgetHostView")' in step.locator.value
                )) and (
                step.action in {ActionType.TAP, ActionType.WAIT_FOR, ActionType.ASSERT_VISIBLE}
            ):
                element = self._find_launcher_widget(step)
            else:
                element = self._find(
                    step.locator, step.target_key, timeout_seconds=step.timeout_seconds,
                    any_visible=step.action in {ActionType.WAIT_FOR, ActionType.ASSERT_VISIBLE, ActionType.ASSERT_PAGE},
                )
            if step.action == ActionType.TAP:
                tap_count = int(step.metadata.get("tap_count", 1))
                action_recovered = False
                try:
                    if tap_count == 1:
                        if step.metadata.get("plan_row_tap") and self.platform == DevicePlatform.ANDROID:
                            checkbox_resource = str(step.metadata.get(
                                "plan_checkbox_resource_id", "com.example.app1:id/example_element_008"
                            ))
                            selection_xpath = step.metadata.get("plan_selection_xpath")
                            row_xpath = step.metadata.get("plan_row_xpath")
                            if not selection_xpath or not row_xpath:
                                raise ValueError("计划选择缺少勾选框定位")
                            # UiScrollable can return an element id that is
                            # detached as the RecyclerView settles. Resolve the
                            # exact named row afresh on each read and require a
                            # stable visible row before using its checkbox.
                            plan_row = self._wait_for_visible_plan_row(
                                row_xpath, timeout_seconds=step.timeout_seconds
                            )
                            checkbox = self._checkbox_for_plan_row(plan_row, selection_xpath)
                            checked = str(checkbox.get_attribute("checked") or "").lower() == "true"
                            was_checked = checked
                            if not checked:
                                rect = checkbox.rect
                                tap_x = round(rect["x"] + rect["width"] / 2)
                                tap_y = round(rect["y"] + rect["height"] / 2)
                                self.event_sink({
                                    "type": "coordinate_tap",
                                    "step_id": step.step_id,
                                    "x": tap_x,
                                    "y": tap_y,
                                    "rect": rect,
                                    "method": "adb" if self.coordinate_tapper is not None else "appium",
                                })
                                if self.coordinate_tapper is not None:
                                    self.coordinate_tapper(tap_x, tap_y)
                                else:
                                    self.driver.execute_script(
                                        "mobile: clickGesture", {"x": tap_x, "y": tap_y}
                                    )
                                deadline = time.monotonic() + step.timeout_seconds
                                while True:
                                    try:
                                        plan_row = self._plan_row_for_name(element, row_xpath)
                                        checkbox = self._checkbox_for_plan_row(plan_row, selection_xpath)
                                        checked = str(checkbox.get_attribute("checked") or "").lower() == "true"
                                        if checked:
                                            break
                                    except Exception as exc:
                                        if _is_environment_error(exc):
                                            raise
                                    if time.monotonic() >= deadline:
                                        raise RuntimeError("点击计划行后勾选状态仍未更新；已阻止继续确认")
                                    time.sleep(0.2)
                            self.event_sink({
                                "type": "plan_row_selection",
                                "step_id": step.step_id,
                                "already_checked": was_checked,
                                "method": "row_geometry_scoped_checkbox_tap",
                                "checkbox_rect": checkbox.rect,
                            })
                        elif step.metadata.get("coordinate_tap") and self.platform == DevicePlatform.ANDROID:
                            tap_element = element
                            child_xpath = step.metadata.get("coordinate_tap_xpath")
                            if child_xpath:
                                # UiScrollable may return before the fling has fully settled.
                                time.sleep(0.4)
                                tap_element = self.driver.find_element(AppiumBy.XPATH, child_xpath)
                            rect = tap_element.rect
                            x_ratio = float(step.metadata.get("tap_x_ratio", 0.5))
                            y_ratio = float(step.metadata.get("tap_y_ratio", 0.5))
                            tap_x = round(rect["x"] + rect["width"] * x_ratio)
                            tap_y = round(rect["y"] + rect["height"] * y_ratio)
                            self.event_sink({
                                "type": "coordinate_tap",
                                "step_id": step.step_id,
                                "x": tap_x,
                                "y": tap_y,
                                "rect": rect,
                                "method": "adb" if self.coordinate_tapper is not None else "appium",
                            })
                            if self.coordinate_tapper is not None:
                                self.coordinate_tapper(tap_x, tap_y)
                            else:
                                self.driver.execute_script(
                                    "mobile: clickGesture",
                                    {"x": tap_x, "y": tap_y},
                                )
                            if step.metadata.get("coordinate_tap_expect_checked") and tap_element is not element:
                                deadline = time.monotonic() + step.timeout_seconds
                                while True:
                                    try:
                                        current_checkbox = self.driver.find_element(
                                            AppiumBy.XPATH, child_xpath
                                        )
                                        checked = str(
                                            current_checkbox.get_attribute("checked") or ""
                                        ).lower()
                                        if checked == "true":
                                            break
                                    except Exception as exc:
                                        if _is_environment_error(exc):
                                            raise
                                    if time.monotonic() >= deadline:
                                        raise RuntimeError(
                                            "计划选择框点击后仍未勾选；已阻止继续确认"
                                        )
                                    time.sleep(0.2)
                        else:
                            element.click()
                    elif tap_count == 2:
                        if self.platform == DevicePlatform.IOS:
                            self.driver.execute_script(
                                "mobile: doubleTap",
                                {"element": element.id},
                            )
                        else:
                            self.driver.execute_script(
                                "mobile: doubleClickGesture",
                                {"elementId": element.id},
                            )
                    else:
                        raise ValueError(f"tap_count 只支持 1 或 2，实际为 {tap_count}")
                except Exception as exc:
                    action_recovered = self._recover_ambiguous_action(
                        step,
                        exc,
                        failure_reason="ambiguous_tap_timeout",
                        verified_reason="ambiguous_tap_timeout_verified",
                    )
                    if not action_recovered:
                        raise
                if action_recovered:
                    return
                self._wait_after_tap(step, step.target_key)
            elif step.action == ActionType.INPUT_TEXT:
                element.clear()
                element.send_keys(step.value)
            elif step.action == ActionType.WAIT_FOR:
                return
            elif step.action == ActionType.ASSERT_VISIBLE:
                assert element.is_displayed(), f"控件不可见：{step.target_key or step.locator.value}"
            elif step.action == ActionType.ASSERT_ENABLED:
                assert element.is_enabled(), f"控件不可用：{step.target_key or step.locator.value}"
            elif step.action == ActionType.ASSERT_PAGE:
                assert element.is_displayed(), f"页面标志不可见：{step.target_key or step.locator.value}"
            else:
                raise ValueError(f"不支持的动作：{step.action}")

    def _wait_for_visible_plan_row(self, row_xpath: str, *, timeout_seconds: float):
        deadline = time.monotonic() + timeout_seconds
        previous = None
        stable_reads = 0
        while True:
            try:
                rows = self.driver.find_elements(AppiumBy.XPATH, row_xpath)
                visible_rows = [row for row in rows if row.is_displayed()]
                if len(visible_rows) > 1:
                    raise LookupError(
                        f"滚动后仍有多个同名计划行可见（{len(visible_rows)} 个），不能猜选"
                    )
                if visible_rows:
                    element = visible_rows[0]
                    rect = element.rect
                else:
                    rect = None
            except Exception as exc:
                if "stale" not in str(exc).lower():
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"计划列表滚动后行元素持续失效：{exc}") from exc
                previous = None
                stable_reads = 0
                time.sleep(0.1)
                continue
            if rect is None:
                if time.monotonic() >= deadline:
                    raise TimeoutError("计划列表滚动后未找到可见的目标计划行")
                time.sleep(0.1)
                continue
            current = tuple(rect[key] for key in ("x", "y", "width", "height"))
            if current == previous:
                stable_reads += 1
                if stable_reads >= 2:
                    return element
            else:
                previous = current
                stable_reads = 0
            if time.monotonic() >= deadline:
                raise TimeoutError(f"计划列表滚动后位置未稳定：{rect}")
            time.sleep(0.1)

    def _plan_row_for_name(self, name_element, row_xpath: str):
        name_rect = name_element.rect
        center_x = name_rect["x"] + name_rect["width"] / 2
        center_y = name_rect["y"] + name_rect["height"] / 2
        rows = self.driver.find_elements(AppiumBy.XPATH, row_xpath)
        matches = []
        for row in rows:
            rect = row.rect
            if (
                rect["x"] <= center_x <= rect["x"] + rect["width"]
                and rect["y"] <= center_y <= rect["y"] + rect["height"]
            ):
                matches.append(row)
        if len(matches) != 1:
            raise LookupError(
                f"计划名称应唯一对应一条计划行，实际找到 {len(matches)} 条；"
                f"名称范围={name_rect}，候选数={len(rows)}"
            )
        return matches[0]

    def _checkbox_for_plan_row(self, row, selection_xpath: str):
        row_rect = row.rect
        candidates = self.driver.find_elements(AppiumBy.XPATH, selection_xpath)
        matches = []
        for candidate in candidates:
            rect = candidate.rect
            center_x = rect["x"] + rect["width"] / 2
            center_y = rect["y"] + rect["height"] / 2
            if (
                row_rect["x"] <= center_x <= row_rect["x"] + row_rect["width"]
                and row_rect["y"] <= center_y <= row_rect["y"] + row_rect["height"]
            ):
                matches.append(candidate)
        if len(matches) != 1:
            raise LookupError(
                f"滚动命中的计划行范围内应有一个勾选框，实际找到 {len(matches)} 个；"
                f"计划行范围={row_rect}，候选数={len(candidates)}"
            )
        return matches[0]

    def _tap_different_color(self, step: PlanStep) -> None:
        deadline = time.monotonic() + step.timeout_seconds
        while True:
            try:
                swatches = self._find_all(step.locator, step.target_key)
                selected_elements = self.driver.find_elements(
                    AppiumBy.XPATH, SELECTED_COLOR_XPATH
                )
                index, swatch = select_unchecked_color(swatches, selected_elements)
                swatch.click()
                break
            except Exception as exc:
                if "stale" not in str(exc).lower() or time.monotonic() >= deadline:
                    raise
                self.event_sink({
                    "type": "stale_color_palette_refreshed",
                    "step_id": step.step_id,
                    "message": str(exc),
                })
                time.sleep(0.2)
        deadline = time.monotonic() + step.timeout_seconds
        while True:
            try:
                current = self._find_all(step.locator, step.target_key)
                selected_elements = self.driver.find_elements(AppiumBy.XPATH, SELECTED_COLOR_XPATH)
                selected = _selected_color_indexes(current, selected_elements)
            except Exception as exc:
                if "stale" not in str(exc).lower() or time.monotonic() >= deadline:
                    raise
                self.event_sink({"type": "stale_color_palette_refreshed", "step_id": step.step_id,
                                 "message": str(exc)})
                time.sleep(0.2)
                continue
            if selected == [index]:
                self.event_sink({"type": "color_change_confirmed", "step_id": step.step_id,
                                 "selected_swatch": index})
                return
            if time.monotonic() >= deadline:
                raise AssertionError(f"颜色未切换到新色块：目标 {index}，当前 {selected}")
            time.sleep(0.2)

    def _wait_after_tap(self, step: PlanStep, target_key: str | None) -> None:
        postcondition = step.metadata.get("postcondition")
        declared = isinstance(postcondition, dict)
        marker = post_tap_page_marker(target_key, self.platform)
        if not declared and marker:
            postcondition = {"type": "page", "target_key": marker}
        if not isinstance(postcondition, dict):
            time.sleep(self.settle_seconds)
            return
        oracle = Oracle.model_validate({
            **postcondition, "oracle_id": f"{step.step_id}-postcondition",
            "title": step.title, "timeout_seconds": step.timeout_seconds,
        })
        passed, message = self._evaluate_oracle(oracle)
        if not passed:
            if not declared or step.postcondition_policy == "advisory":
                self.event_sink({"type": "postcondition_observed_missing", "step_id": step.step_id,
                                 "message": message, "source": "plan" if declared else "catalog"})
                time.sleep(self.settle_seconds)
                return
            raise PostconditionFailure(f"操作后等待条件未满足：{message}")
        self.event_sink(
            {
                "type": "page_transition_confirmed",
                "step_id": step.step_id,
                "target_key": oracle.target_key,
            }
        )

    def _get_window_size_with_recovery(self, step: PlanStep) -> dict[str, int]:
        """Refresh a stale Appium session before calculating swipe coordinates.

        On real Android devices UiAutomator2 may time out while reading the
        viewport even though the device is still reachable.  The previous
        implementation only recovered failures from the swipe call itself,
        so a timeout in get_window_size escaped before recovery was possible.
        """
        try:
            return self.driver.get_window_size()
        except Exception as exc:
            if not _is_environment_error(exc) or not self.driver_refresher:
                raise
            self.event_sink(
                {
                    "type": "driver_refresh",
                    "reason": "proxy_timeout_recovery",
                    "phase": "swipe_viewport",
                    "step_id": step.step_id,
                    "message": str(exc),
                }
            )
            self.driver = self.driver_refresher(self.driver, self.settle_seconds)
            return self.driver.get_window_size()

    def _recover_ambiguous_swipe(self, step: PlanStep, exc: Exception) -> bool:
        """Accept a timed-out swipe only when its declared postcondition is already true.

        UiAutomator2 can perform a gesture on the device but time out while returning the
        response. Repeating that gesture blindly may advance a carousel twice, so plans
        that want safe recovery must declare a postcondition in step metadata.
        """
        return self._recover_ambiguous_action(
            step,
            exc,
            failure_reason="ambiguous_swipe_timeout",
            verified_reason="ambiguous_swipe_timeout_verified",
        )

    def _recover_ambiguous_action(
        self,
        step: PlanStep,
        exc: Exception,
        *,
        failure_reason: str,
        verified_reason: str,
    ) -> bool:
        """Verify the declared state before accepting an action whose response timed out."""
        postcondition = step.metadata.get("postcondition")
        if (
            not _is_environment_error(exc)
            or not self.driver_refresher
            or not isinstance(postcondition, dict)
        ):
            return False
        try:
            oracle = Oracle(
                oracle_id=f"{step.step_id}-timeout-postcondition",
                title=f"{step.title}超时后的状态确认",
                type=OracleType(postcondition["type"]),
                target_key=postcondition.get("target_key"),
                locator=postcondition.get("locator"),
                expected=postcondition.get("expected"),
            )
            self.driver = self.driver_refresher(self.driver, self.settle_seconds)
            passed, message = self._evaluate_oracle_once(oracle)
        except Exception as recovery_exc:
            self.event_sink(
                {
                    "type": "driver_recovery_failed",
                    "reason": failure_reason,
                    "step_id": step.step_id,
                    "message": str(recovery_exc),
                }
            )
            return False
        if not passed:
            return False
        self.event_sink(
            {
                "type": "driver_refresh",
                "reason": verified_reason,
                "phase": "step",
                "step_id": step.step_id,
                "message": message,
            }
        )
        return True

    def _find(
        self,
        locator: Locator | None,
        target_key: str | None = None,
        *,
        timeout_seconds: float = 5,
        allow_driver_recovery: bool = True,
        any_visible: bool = False,
    ):
        candidates = self._target_candidates(target_key, locator)
        if not candidates:
            raise ValueError("缺少定位目标")
        last_error: Exception | None = None
        ambiguity: AmbiguousLocatorError | None = None
        deadline = time.monotonic() + timeout_seconds
        while True:
            for candidate in candidates:
                try:
                    by, value = self._appium_locator(candidate, self.platform)
                    finder = getattr(self.driver, "find_elements", None)
                    if finder is None:
                        return self.driver.find_element(by, value)
                    elements = finder(by, value)
                    if any_visible:
                        if not elements:
                            try:
                                return self.driver.find_element(by, value)
                            except Exception as exc:
                                last_error = exc
                                continue
                        visible = [element for element in elements if element.is_displayed()]
                        if visible:
                            return visible[0]
                        continue
                    if len(elements) == 1:
                        return elements[0]
                    if len(elements) > 1:
                        ambiguity = AmbiguousLocatorError(
                            f"定位 {target_key or locator} 命中 {len(elements)} 个元素，未提供对象身份条件"
                        )
                        continue
                    # Keep compatibility with lightweight test/fallback drivers;
                    # Appium itself returns the same lookup failure here.
                    return self.driver.find_element(by, value)
                except ValueError:
                    raise
                except Exception as exc:
                    if isinstance(exc, AmbiguousLocatorError):
                        ambiguity = exc
                        continue
                    last_error = exc
                    if _is_environment_error(exc) and self.driver_refresher and allow_driver_recovery:
                        self.event_sink(
                            {
                                "type": "driver_refresh",
                                "reason": "proxy_timeout_recovery",
                                "message": str(exc),
                            }
                        )
                        self.driver = self.driver_refresher(self.driver, self.settle_seconds)
                        remaining = max(0.2, deadline - time.monotonic())
                        return self._find(
                            locator,
                            target_key,
                            timeout_seconds=remaining,
                            allow_driver_recovery=False,
                            any_visible=any_visible,
                        )
            if time.monotonic() >= deadline:
                break
            time.sleep(min(0.2, max(0, deadline - time.monotonic())))
        if last_error is not None and _is_environment_error(last_error):
            raise last_error
        if ambiguity is not None:
            raise ambiguity
        raise TargetNotFoundError(f"所有候选定位均失败：{target_key or locator}; {last_error}")

    @staticmethod
    def _appium_locator(
        locator: Locator, platform: DevicePlatform | str = DevicePlatform.ANDROID
    ) -> tuple[str, str]:
        selected = DevicePlatform(platform)
        if locator.strategy == LocatorStrategy.RESOURCE_ID:
            if selected == DevicePlatform.IOS:
                raise ValueError("resource_id 是 Android 定位策略，iOS 请使用 accessibility_id 或 iOS 定位器")
            return AppiumBy.ID, locator.value
        if locator.strategy == LocatorStrategy.ACCESSIBILITY_ID:
            return AppiumBy.ACCESSIBILITY_ID, locator.value
        if locator.strategy == LocatorStrategy.TEXT:
            if selected == DevicePlatform.IOS:
                escaped = locator.value.replace("\\", "\\\\").replace("'", "\\'")
                return (
                    AppiumBy.IOS_PREDICATE,
                    f"name == '{escaped}' OR label == '{escaped}' OR value == '{escaped}'",
                )
            return AppiumBy.ANDROID_UIAUTOMATOR, f"new UiSelector().text({json.dumps(locator.value, ensure_ascii=False)})"
        if locator.strategy == LocatorStrategy.ANDROID_UIAUTOMATOR:
            if selected == DevicePlatform.IOS:
                raise ValueError("android_uiautomator 不能用于 iOS")
            return AppiumBy.ANDROID_UIAUTOMATOR, locator.value
        if locator.strategy == LocatorStrategy.XPATH:
            return AppiumBy.XPATH, locator.value
        if locator.strategy == LocatorStrategy.IOS_PREDICATE:
            if selected == DevicePlatform.ANDROID:
                raise ValueError("ios_predicate 不能用于 Android")
            return AppiumBy.IOS_PREDICATE, locator.value
        if locator.strategy == LocatorStrategy.IOS_CLASS_CHAIN:
            if selected == DevicePlatform.ANDROID:
                raise ValueError("ios_class_chain 不能用于 Android")
            return AppiumBy.IOS_CLASS_CHAIN, locator.value
        if locator.strategy == LocatorStrategy.NORMALIZED_COORDINATE:
            raise ValueError("归一化坐标只能用于经过验证的修复动作，不能作为普通元素定位器")
        raise ValueError(f"不支持的定位策略：{locator.strategy}")

    def _evaluate_oracle(self, oracle: Oracle) -> tuple[bool, str]:
        if oracle.type == OracleType.VISUAL_ASSERTION:
            return self._evaluate_oracle_once(oracle)
        # Re-read changing state only; never repeat the action or scroll a list here.
        if not oracle.scroll_container and oracle.type in {OracleType.TEXT_EQUALS, OracleType.DATE_COUNTDOWN,
                           OracleType.ENABLED, OracleType.DISABLED}:
            deadline = time.monotonic() + oracle.timeout_seconds
            while True:
                try:
                    outcome = self._evaluate_oracle_once(oracle, timeout_seconds=0)
                except Exception as exc:
                    if _is_environment_error(exc):
                        raise
                    outcome = (False, f"未找到或无法读取验收目标：{exc}")
                if outcome[0] or time.monotonic() >= deadline:
                    return outcome
                time.sleep(min(0.2, max(0, deadline - time.monotonic())))
        try:
            return self._evaluate_oracle_once(oracle, timeout_seconds=oracle.timeout_seconds)
        except Exception as exc:
            if _is_environment_error(exc) and self.driver_refresher:
                self.event_sink(
                    {
                        "type": "driver_refresh",
                        "reason": "proxy_timeout_recovery",
                        "phase": "oracle",
                        "oracle_id": oracle.oracle_id,
                        "message": str(exc),
                    }
                )
                self.driver = self.driver_refresher(self.driver, self.settle_seconds)
                try:
                    return self._evaluate_oracle_once(oracle)
                except Exception as retry_exc:
                    exc = retry_exc
            if _is_environment_error(exc):
                raise exc
            if oracle.scroll_container:
                return False, f"列表查找未完成：{exc}"
            if oracle.type == OracleType.NOT_VISIBLE and isinstance(exc, LookupError):
                return True, "目标未出现"
            if oracle.type == OracleType.PAGE and isinstance(exc, LookupError):
                return False, "预期页面未出现"
            if (
                oracle.type == OracleType.VISIBLE
                and isinstance(exc, LookupError)
                and oracle.locator is not None
                and oracle.locator.strategy == LocatorStrategy.TEXT
            ):
                return False, f"需求指定文案未出现：{oracle.locator.value}"
            if oracle.type in {OracleType.TEXT_SEQUENCE, OracleType.TEXT_CONTAINS_ALL}:
                return False, f"读取文本顺序失败：{exc}"
            return False, f"未找到或无法读取验收目标：{exc}"

    def _evaluate_oracle_once(self, oracle: Oracle, *, timeout_seconds: float = 5) -> tuple[bool, str]:
        if oracle.scroll_container:
            return self._search_list_oracle(oracle)
        if oracle.saved_widget_match and oracle.type == OracleType.VISIBLE:
            self._find_saved_widget(PlanStep(step_id=oracle.oracle_id, title=oracle.title,
                action=ActionType.ASSERT_VISIBLE, target_key=oracle.target_key,
                metadata={"saved_widget_match": oracle.saved_widget_match}))
            return True, f"已找到指定组件：{oracle.saved_widget_match}"
        if oracle.saved_widget_match and oracle.type in {OracleType.NOT_VISIBLE, OracleType.ELEMENT_COUNT}:
            count = len(self._saved_widget_rows(oracle.saved_widget_match))
            passed = (count > 0 if oracle.type == OracleType.VISIBLE else count == 0
                      if oracle.type == OracleType.NOT_VISIBLE else count == oracle.expected)
            return passed, f"指定组件 {oracle.saved_widget_match} 在完整列表中匹配 {count} 条"
        if oracle.saved_widget_match and oracle.type != OracleType.ELEMENT_COUNT_DELTA:
            raise ValueError("此验收类型不支持组件范围筛选")
        if oracle.type == OracleType.VISUAL_ASSERTION:
            if self.visual_oracle_handler is None:
                raise RuntimeError("视觉验收模型未配置")
            passed, message, details = self.visual_oracle_handler(oracle, self.driver)
            self.event_sink(
                {
                    "type": "visual_oracle_result",
                    "oracle_id": oracle.oracle_id,
                    "passed": passed,
                    **details,
                }
            )
            return passed, message
        if oracle.type == OracleType.TEXT_SEQUENCE:
            expected = list(oracle.expected) if isinstance(oracle.expected, list) else []
            try:
                actual = self._collect_text_sequence(
                    oracle.locator, oracle.target_key, len(expected), must_reach_end=True,
                    expected_prefix=expected,
                )
            except SequencePrefixMismatch as exc:
                return False, f"起始顺序已不符：期望 {expected}；首屏确认 {exc.observed}；未推断后续卡片"
            passed = actual == expected
            return passed, f"期望顺序：{expected}；实际顺序：{actual}"
        if oracle.type == OracleType.TEXT_CONTAINS_ALL:
            expected = list(oracle.expected) if isinstance(oracle.expected, list) else []
            actual = self._collect_text_sequence(
                oracle.locator,
                oracle.target_key,
                None,
                required_texts=set(expected),
            )
            missing = [text for text in expected if text not in actual]
            return not missing, f"必须包含：{expected}；实际内容：{actual}；缺少：{missing}"
        if oracle.type == OracleType.ELEMENT_COUNT:
            expected = int(oracle.expected) if isinstance(oracle.expected, int) else -1
            actual = len(self._collect_text_sequence(
                oracle.locator, oracle.target_key, None, must_reach_end=True,
                include_empty=True,
            ))
            return actual == expected, f"期望数量：{expected}；实际数量：{actual}"
        if oracle.type == OracleType.ELEMENT_COUNT_DELTA:
            expected_delta = int(oracle.expected) if isinstance(oracle.expected, int) else -1
            baseline = self._captured_element_counts.get(oracle.reference_step_id or "")
            if baseline is None:
                return False, f"未找到步骤 {oracle.reference_step_id} 的基线数量"
            actual = len(self._saved_widget_rows(oracle.saved_widget_match)) if oracle.saved_widget_match else (
                len(self._find_all(oracle.locator, oracle.target_key))
                if oracle.reference_step_id in self._captured_visible_counts
                else len(self._collect_text_sequence(
                    oracle.locator, oracle.target_key, None, must_reach_end=True, include_empty=True,
                ))
            )
            delta = actual - baseline
            scope = "当前可见列表" if oracle.reference_step_id in self._captured_visible_counts else "完整列表"
            return delta == expected_delta, f"{scope}预期数量变化：{expected_delta}；基线：{baseline}；当前：{actual}；变化：{delta}"

        if oracle.type == OracleType.TEXT_EQUALS:
            expected = str(oracle.expected)
            matches = self._find_all(oracle.locator, oracle.target_key)
            if not matches:
                try:
                    matches = [self._find(oracle.locator, oracle.target_key, timeout_seconds=timeout_seconds)]
                except LookupError:
                    matches = []
            actual = [self._element_text(item) for item in matches if item.is_displayed()]
            passed = _normalize_ui_text(expected) in [_normalize_ui_text(value) for value in actual]
            return passed, f"期望文本：{expected}；实际可见文本：{actual}"

        element = self._find(
            oracle.locator, oracle.target_key,
            timeout_seconds=timeout_seconds,
            any_visible=oracle.type in {OracleType.VISIBLE, OracleType.PAGE},
        )
        if oracle.type in {OracleType.VISIBLE, OracleType.PAGE}:
            displayed = bool(element.is_displayed())
            return displayed, "目标可见" if displayed else "目标不可见"
        if oracle.type == OracleType.NOT_VISIBLE:
            return False, "不应出现的目标仍然可见"
        if oracle.type == OracleType.ENABLED:
            enabled = bool(element.is_enabled())
            return enabled, "控件可用" if enabled else "控件不可用"
        if oracle.type == OracleType.DISABLED:
            enabled = bool(element.is_enabled())
            return not enabled, "控件已禁用" if not enabled else "控件仍可用"
        if oracle.type == OracleType.DATE_COUNTDOWN:
            actual = _normalize_ui_text(element.text)
            departure = date.fromisoformat(str(oracle.expected))
            expected_days = max(0, (departure - date.today()).days)
            return actual == str(expected_days), f"出发日期：{departure.isoformat()}；期望倒计时：{expected_days}；实际文本：{actual}"
        return False, f"不支持的验收类型：{oracle.type}"

    def _search_list_oracle(self, oracle: Oracle) -> tuple[bool, str]:
        if self.platform != DevicePlatform.ANDROID:
            raise ValueError("列表查找当前仅支持 Android")
        if oracle.type not in {OracleType.VISIBLE, OracleType.TEXT_EQUALS} or oracle.saved_widget_match:
            raise ValueError("此验收类型不支持列表查找")
        deadline = time.monotonic() + oracle.timeout_seconds
        container = self._find(oracle.scroll_container, timeout_seconds=0)

        def found() -> bool:
            for item in self._find_all(oracle.locator, oracle.target_key):
                if not item.is_displayed():
                    continue
                if not any(child.id == item.id for child in container.find_elements(AppiumBy.XPATH, ".//*")):
                    continue
                if oracle.type == OracleType.VISIBLE or _normalize_ui_text(self._element_text(item)) == _normalize_ui_text(str(oracle.expected)):
                    return True
            return False

        if found():
            return True, "在指定列表找到本轮目标"
        # Search from the top so a previous check's scroll position cannot hide the target.
        for direction in ("up", "down"):
            unchanged = 0
            while time.monotonic() < deadline:
                before = self.driver.page_source
                rect, screen = container.rect, self.driver.get_window_size()
                left, top = max(0, rect["x"]), max(0, rect["y"])
                right = min(screen["width"], rect["x"] + rect["width"])
                bottom = min(screen["height"], rect["y"] + rect["height"])
                if right <= left or bottom <= top:
                    raise LookupError("指定列表不在屏幕内，无法继续查找")
                x = round((left + right) / 2)
                y1, y2 = round(top + (bottom-top)*0.8), round(top + (bottom-top)*0.2)
                if direction == "up":
                    y1, y2 = y2, y1
                self.driver.swipe(x, y1, x, y2, 500)
                if self.settle_seconds:
                    time.sleep(min(self.settle_seconds, 1))
                changed = self.driver.page_source != before
                self.event_sink({"type": "list_target_search", "oracle_id": oracle.oracle_id,
                                 "direction": direction, "viewport_changed": changed})
                if found():
                    return True, "滚动指定列表后找到本轮目标"
                unchanged = 0 if changed else unchanged + 1
                if unchanged >= 2:
                    break
            else:
                return False, "查找超时，尚未确认列表到底，无法确认本轮目标"
        return False, "指定列表滚动后页面不再变化，仍未找到本轮目标"

    def _collect_text_sequence(
        self,
        locator: Locator | None,
        target_key: str | None,
        expected_count: int | None,
        required_texts: set[str] | None = None,
        *,
        must_reach_end: bool = False,
        include_empty: bool = False,
        expected_prefix: list[str] | None = None,
        saved_widget_rows: bool = False,
        match_scope: dict[str, str] | None = None,
    ) -> list[str]:
        """Read a scrollable list, preserving repeated rows and removing viewport overlap."""
        collected: list[str] = []
        previous_viewport: list[tuple[str, ...]] | None = None
        complete = False
        initial = self._find_all(locator, target_key)
        container = None
        if saved_widget_rows:
            containers = self.driver.find_elements(AppiumBy.ID, "com.example.app1:id/example_element_079")
            if not containers:
                if self._find_all(None, "widget.empty_state"):
                    return []
                raise LookupError("未找到已保存组件列表，不能将未加载或错误页面当成零条")
            container = containers[0]
        if container is None and initial and self.platform == DevicePlatform.ANDROID and hasattr(initial[0], "find_elements"):
            parents = initial[0].find_elements(AppiumBy.XPATH, "./ancestor::*[@scrollable='true'][1]")
            if not parents:
                if target_key in {"saved_widget.name", "saved_widget.plan_name"} or (
                    locator is not None and "com.example.app1:id/example_element_149" in locator.value
                ):
                    parents = self.driver.find_elements(AppiumBy.ID, "com.example.app1:id/example_element_079")
                elif target_key == "widget.card_names":
                    parents = self.driver.find_elements(AppiumBy.ID, "com.example.app1:id/example_element_076")
            if not parents:
                if must_reach_end:
                    raise LookupError("未找到可确认滚动边界的列表，不能证明读取完整")
                return [self._element_text(item) for item in initial
                        if include_empty or self._element_text(item)]
            container = parents[0]
        deadline = time.monotonic() + (120 if saved_widget_rows else 60)
        if container is not None:
            while True:
                can_scroll_more = self._scroll_list(container, "up")
                self.event_sink({
                    "type": "list_scan_reset",
                    "direction": "up",
                    "can_scroll_more": can_scroll_more,
                })
                if not can_scroll_more:
                    break
                if time.monotonic() >= deadline:
                    raise LookupError("列表未能回到起点，不能给出全量验收结果")
        at_end = False
        while time.monotonic() < deadline:
            elements = self._find_all(locator, target_key)
            viewport_items = [
                (self._element_text(element), self._element_identity(element))
                for element in elements
                if include_empty or self._element_text(element)
            ]
            if saved_widget_rows:
                viewport_items = [(row, (row,)) for row in self._saved_widget_viewport(self.driver.page_source)]
                if elements and not viewport_items:
                    raise LookupError("组件卡片字段未读取完整，不能作为零条结果")
            viewport_keys = [item[1] for item in viewport_items]
            viewport_texts = [item[0] for item in viewport_items]
            if expected_prefix is not None and not collected:
                for index, actual_text in enumerate(viewport_texts[:len(expected_prefix)]):
                    if actual_text != expected_prefix[index]:
                        raise SequencePrefixMismatch(viewport_texts[:index + 1])
            if not viewport_texts:
                complete = True
                break
            if at_end and viewport_keys == previous_viewport:
                complete = True
                break
            overlap = 0
            if previous_viewport:
                overlaps = []
                for size in range(min(len(previous_viewport), len(viewport_keys)), 0, -1):
                    if previous_viewport[-size:] == viewport_keys[:size]:
                        overlaps.append(size)
                if container is not None and (len(overlaps) > 1 or viewport_keys == previous_viewport):
                    # Duplicate unrelated cards cannot change the count of our target.
                    # If any ambiguous row belongs to the target, keep the ambiguity.
                    ambiguous = viewport_texts[:max(overlaps, default=len(viewport_texts))]
                    if not match_scope or any(all(json.loads(row).get(key) == value
                                                  for key, value in match_scope.items()) for row in ambiguous):
                        raise LookupError("滚动后条目内容无法区分，不能可靠合并或判定列表已到底")
                overlap = overlaps[0] if overlaps else 0
            collected.extend(viewport_texts[overlap:])
            self.event_sink({
                "type": "list_scan_viewport",
                "viewport_texts": viewport_texts,
                "overlap_count": overlap,
                "collected_count": len(collected),
                "at_end": at_end,
            })
            if (
                (not must_reach_end and expected_count is not None and len(collected) >= expected_count)
                or (not must_reach_end and required_texts is not None and required_texts.issubset(collected))
                or (container is None and viewport_keys == previous_viewport)
            ):
                complete = True
                break
            previous_viewport = viewport_keys
            if container is not None:
                can_scroll_more = self._scroll_list(container, "down")
                at_end = not can_scroll_more
                self.event_sink({
                    "type": "list_scan_advance",
                    "direction": "down",
                    "can_scroll_more": can_scroll_more,
                    "at_end": at_end,
                })
                continue
            size = self.driver.get_window_size()
            x1 = round(size["width"] * 0.5)
            y1 = round(size["height"] * 0.78)
            x2 = round(size["width"] * 0.5)
            y2 = round(size["height"] * 0.32)
            try:
                self.driver.swipe(x1, y1, x2, y2, 500)
            except Exception as exc:
                if self.platform != DevicePlatform.ANDROID or "w3c actions" not in str(exc).lower():
                    raise
                distance = max(abs(x2 - x1), abs(y2 - y1), 1)
                self.driver.execute_script(
                    "mobile: dragGesture",
                    {"startX": x1, "startY": y1, "endX": x2, "endY": y2, "speed": distance * 2},
                )
            if self.settle_seconds:
                time.sleep(min(self.settle_seconds, 1))
        if must_reach_end and not complete:
            raise LookupError("列表在规定采集视口内未能确认遍历完整，不能给出全量验收结果")
        return collected

    def _scroll_list(self, container: Any, direction: str) -> bool:
        more = self.driver.execute_script("mobile: scrollGesture", {
            "elementId": container.id, "direction": direction, "percent": 0.65,
        })
        if not isinstance(more, bool):
            raise LookupError("设备未返回列表滚动边界，不能确认读取完整")
        if self.settle_seconds:
            time.sleep(min(self.settle_seconds, 1))
        return more

    @staticmethod
    def _element_text(element: Any) -> str:
        return str(getattr(element, "text", "") or "").strip()

    @staticmethod
    def _element_identity(element: Any) -> tuple[str, ...]:
        values = [str(getattr(element, "text", "") or "").strip()]
        for attribute in ("resource-id", "content-desc", "className"):
            try:
                values.append(str(element.get_attribute(attribute) or ""))
            except Exception:
                values.append("")
        return tuple(values)

    def _find_all(self, locator: Locator | None, target_key: str | None = None, *, allow_driver_recovery: bool = True):
        candidates = self._target_candidates(target_key, locator)
        if not candidates:
            raise ValueError("缺少定位目标")
        last_error: Exception | None = None
        for candidate in candidates:
            try:
                by, value = self._appium_locator(candidate, self.platform)
                elements = self.driver.find_elements(by, value)
                if elements:
                    return elements
            except Exception as exc:
                last_error = exc
                if _is_environment_error(exc) and self.driver_refresher and allow_driver_recovery:
                    self.event_sink({"type": "driver_refresh", "reason": "proxy_timeout_recovery", "message": str(exc)})
                    self.driver = self.driver_refresher(self.driver, self.settle_seconds)
                    return self._find_all(locator, target_key, allow_driver_recovery=False)
        if last_error:
            raise last_error
        return []

    @staticmethod
    def _saved_widget_viewport(page_source: str) -> list[str]:
        root = ET.fromstring(page_source)
        rows = []
        for parent in root.iter():
            names = [node.get("text", "") for node in parent
                     if node.get("resource-id") == "com.example.app1:id/example_element_149"]
            plans = [node.get("text", "") for node in parent
                     if node.get("resource-id") == "com.example.app1:id/example_element_132"]
            # A clipped edge row is read when scrolling brings both fields into view.
            if len(names) == len(plans) == 1:
                rows.append(json.dumps({"widget_name": names[0], "plan_name": plans[0]}, ensure_ascii=False))
        return rows

    def _saved_widget_rows(self, match: dict[str, str]) -> list[str]:
        saved_widget_locator("saved_widget.name", match)
        if self.platform != DevicePlatform.ANDROID:
            raise LookupError("当前组件行筛选仅有 Android 页面证据")
        rows = self._collect_text_sequence(None, "saved_widget.name", None,
                                          must_reach_end=True, saved_widget_rows=True, match_scope=match)
        return [row for row in rows if all(json.loads(row).get(key) == value for key, value in match.items())]

    def _find_saved_widget(self, step: PlanStep):
        match = step.metadata["saved_widget_match"]
        locator = saved_widget_locator(step.target_key, match)
        if self.platform != DevicePlatform.ANDROID:
            raise LookupError("当前组件行筛选仅有 Android 页面证据")
        deadline = time.monotonic() + max(step.timeout_seconds, 60)
        reset = False
        while True:
            matches = self._find_all(locator)
            if len(matches) > 1:
                raise AmbiguousLocatorError(f"指定组件 {match} 在当前页面匹配 {len(matches)} 条，无法唯一操作")
            if len(matches) == 1:
                self.event_sink({"type": "saved_widget_identified", "step_id": step.step_id, "match": match})
                return matches[0]
            containers = self.driver.find_elements(AppiumBy.ID, "com.example.app1:id/example_element_079")
            if not containers or time.monotonic() >= deadline:
                break
            if not reset:
                while self._scroll_list(containers[0], "up"):
                    if time.monotonic() >= deadline:
                        raise TargetNotFoundError("组件列表未能回到起点")
                reset = True
                continue
            more = self._scroll_list(containers[0], "down")
            if not more:
                return self._find(locator, timeout_seconds=1)
        raise TargetNotFoundError(f"已核实组件但无法重新定位：{match}")

    def _find_launcher_widget(self, step: PlanStep):
        """Find a desktop widget by its visible identity, never by required size."""
        widgets = self._find_all(step.locator, step.target_key)
        try:
            return self._select_launcher_widget(widgets, step)
        except LookupError as initial_error:
            page, page_count = self._launcher_page_state()
            if page is None or page_count is None or page_count < 2:
                raise initial_error

        screen = self.driver.get_window_size()
        start, current = page, page
        self.event_sink({"type": "launcher_scan_started", "step_id": step.step_id,
                         "page": page, "page_count": page_count})
        selected = None
        try:
            # Walk each page once, returning to the starting page on failure.
            directions = (
                (("left", page_count - page), ("right", page_count - 1))
                if page > page_count / 2
                else (("right", page - 1), ("left", page_count - 1))
            )
            for direction, count in directions:
                for _ in range(count):
                    self._swipe_launcher_page(direction, screen)
                    if self.settle_seconds:
                        time.sleep(min(self.settle_seconds, 0.5))
                    next_page, _ = self._launcher_page_state()
                    self.event_sink({"type": "launcher_page_checked", "step_id": step.step_id,
                                     "before": current, "after": next_page})
                    if next_page is None or next_page == current:
                        break
                    current = next_page
                    widgets = self._find_all(step.locator, step.target_key)
                    try:
                        selected = self._select_launcher_widget(widgets, step)
                        break
                    except LookupError:
                        continue
                if selected is not None:
                    break
        finally:
            if selected is None and current != start:
                direction = "right" if current > start else "left"
                for _ in range(abs(current - start)):
                    self._swipe_launcher_page(direction, screen)
        if selected is not None:
            return selected
        raise LookupError(self._widget_lookup_message(step))

    def _swipe_launcher_page(self, direction: str, screen: dict[str, int]) -> None:
        y = round(screen["height"] * 0.75)
        left, right = round(screen["width"] * 0.25), round(screen["width"] * 0.75)
        swipe = getattr(self.driver, "swipe", None)
        if callable(swipe):
            x1, x2 = (left, right) if direction == "right" else (right, left)
            swipe(x1, y, x2, y, 650)
        else:
            self.driver.execute_script("mobile: swipeGesture", {
                "left": 0, "top": round(screen["height"] * 0.72),
                "width": screen["width"], "height": round(screen["height"] * 0.06),
                "percent": 0.6, "direction": direction,
            })

    def _select_launcher_widget(self, widgets, step: PlanStep):
        widget_type = str(step.metadata.get("widget_type") or "").strip().lower()
        if widget_type:
            aliases = {
                "luggage": ("行李清单", "行李"),
                "reminder": ("出行提醒", "行程提醒"),
                "itinerary": ("今日行程",),
                "bill": ("旅行账单", "账单", "总预算", "已超支"),
                "placeholder": ("点击选择", "心仪的组件"),
            }.get(widget_type)
            if aliases is None:
                raise LookupError(f"不支持的桌面组件类型：{widget_type}")
            matches = [widget for widget in widgets if self._widget_contains_any(widget, aliases)]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise LookupError(f"桌面{widget_type}组件不唯一（{len(matches)} 个）")
            if widgets and self.widget_visual_matcher is not None:
                visual_match = self.widget_visual_matcher(step, widgets, self.driver)
                if visual_match is not None:
                    return visual_match
            raise LookupError(f"当前桌面页未能从可见内容确认{widget_type}组件")
        if len(widgets) == 1:
            return widgets[0]
        if len(widgets) > 1:
            raise LookupError(f"桌面组件身份不明确（{len(widgets)} 个），不能按尺寸或顺序猜测")
        raise LookupError("当前桌面页没有找到 app1 组件")

    def _widget_contains_any(self, widget, aliases: tuple[str, ...]) -> bool:
        texts: list[str] = []
        try:
            texts.append(str(getattr(widget, "text", "") or ""))
        except Exception:
            pass
        for attribute in ("content-desc", "contentDescription"):
            try:
                texts.append(str(widget.get_attribute(attribute) or ""))
            except Exception:
                continue
        try:
            descendants = widget.find_elements(AppiumBy.XPATH, ".//*")
        except Exception:
            descendants = []
        for child in descendants:
            try:
                texts.append(str(getattr(child, "text", "") or ""))
                texts.append(str(child.get_attribute("content-desc") or ""))
            except Exception:
                continue
        combined = " ".join(texts)
        return any(alias in combined for alias in aliases)

    @staticmethod
    def _widget_lookup_message(step: PlanStep) -> str:
        widget_type = str(step.metadata.get("widget_type") or "app1")
        return f"没有从桌面内容确认目标组件（{widget_type}）；请核对组件类型或前置数据"

    def _launcher_page_state(self) -> tuple[int | None, int | None]:
        try:
            indicators = self.driver.find_elements(AppiumBy.ID, "com.example.launcher:id/example_element_159")
        except Exception:
            indicators = []
        for indicator in indicators:
            for attribute in ("content-desc", "contentDescription"):
                try:
                    text = str(indicator.get_attribute(attribute) or "")
                except Exception:
                    continue
                match = re.search(r"第\s*(\d+)\s*页\s*[，,]\s*共\s*(\d+)\s*页", text)
                if match:
                    return int(match.group(1)), int(match.group(2))
        try:
            counters = self.driver.find_elements(AppiumBy.ID, "com.example.launcher:id/example_element_011")
        except Exception:
            counters = []
        for counter in counters:
            try:
                match = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", str(counter.text or ""))
            except Exception:
                continue
            if match:
                return int(match.group(1)), int(match.group(2))
        try:
            page_source = str(self.driver.page_source)
        except Exception:
            return None, None
        match = re.search(r"指示器第\s*(\d+)\s*页\s*[，,]\s*共\s*(\d+)\s*页", page_source)
        if not match:
            match = re.search(r'(?<!\d)(\d+)\s*/\s*(\d+)(?!\d)', page_source)
        if match:
            return int(match.group(1)), int(match.group(2))
        return None, None

    def _target_candidates(
        self, target_key: str | None, locator: Locator | None
    ) -> tuple[Locator, ...]:
        if target_key and target_key in self.locator_snapshot:
            return tuple(self.locator_snapshot[target_key])
        if target_key:
            return resolve_target(target_key, self.platform)
        return (locator,) if locator else ()

    def audit_locators(self, plan: ExecutionPlan) -> None:
        """Report static locator conflicts without changing a frozen plan's selectors."""
        for target_key, saved in self.locator_snapshot.items():
            try:
                current = resolve_target(target_key, self.platform)
            except (KeyError, LookupError) as exc:
                self.event_sink({"type": "locator_snapshot_unverifiable", "target_key": target_key,
                                 "message": str(exc)})
                continue
            if tuple(saved) != current:
                self.event_sink({
                    "type": "locator_snapshot_differs_from_catalog", "target_key": target_key,
                    "saved": [item.model_dump(mode="json") for item in saved],
                    "catalog": [item.model_dump(mode="json") for item in current],
                })
        for step in plan.steps:
            if step.target_key and step.locator is not None:
                self.event_sink({"type": "plan_direct_locator_ignored", "step_id": step.step_id,
                                 "target_key": step.target_key})


SELECTED_COLOR_XPATH = (
    "//*[@resource-id='com.example.app1:id/example_element_153' "
    "and ../*[@resource-id='com.example.app1:id/example_element_023']]"
)


def _selected_color_indexes(elements, selected_elements) -> list[int]:
    selected_rects = {tuple(item.rect[key] for key in ("x", "y", "width", "height")) for item in selected_elements}
    return [index for index, item in enumerate(elements)
            if tuple(item.rect[key] for key in ("x", "y", "width", "height")) in selected_rects]


def select_unchecked_color(elements, selected_elements):
    """Pick another color based on the checkmark, not palette position."""
    current = _selected_color_indexes(elements, selected_elements)
    if len(current) != 1:
        raise LookupError(f"当前颜色选中状态不唯一（{len(current)} 个）")
    for index, swatch in enumerate(elements):
        if index != current[0]:
            return index, swatch
    raise LookupError("未找到未选中的颜色色块")


def select_widget_by_shape(elements, shape: str, *, screen_height: int):
    """Select a unique launcher widget by screen-relative shape, not list position."""
    if shape not in {"wide", "compact", "tall"}:
        raise ValueError(f"不支持的桌面组件形状：{shape}")
    if screen_height <= 0:
        raise ValueError("无法读取设备屏幕高度，不能按组件形状选择")
    matches = []
    for element in elements:
        rect = element.rect
        width = float(rect["width"])
        height = float(rect["height"])
        if height <= 0:
            continue
        observed_shape = (
            "wide" if width / height >= 1.5
            else "tall" if height / screen_height >= 0.35
            else "compact"
        )
        if observed_shape == shape:
            matches.append(element)
    if not matches:
        raise LookupError(f"未找到 {shape} 桌面组件；请先检查该用例前置条件")
    if len(matches) > 1:
        raise LookupError(f"{shape} 桌面组件不唯一（{len(matches)} 个）；不能按顺序猜测")
    return matches[0]


def _is_environment_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in (
        "could not proxy", "connection refused", "invalid session", "session not created",
        "adb device offline", "device unauthorized",
    )) or ("timeout" in message and any(term in message for term in ("proxy", "connection", "session", "command")))


def _normalize_ui_text(value: str) -> str:
    """Treat layout-only whitespace differences as equal in UI text assertions."""
    return " ".join(str(value).replace("\u00a0", " ").split())
