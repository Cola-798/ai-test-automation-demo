from __future__ import annotations

from datetime import date
import re

from .schemas import (
    ActionType,
    ExecutionPlan,
    Locator,
    LocatorStrategy,
    OracleType,
    PlanStep,
    StateMode,
    ValidationIssue,
    ValidationResult,
)
from .locator_catalog import target_exists, saved_widget_locator
from .constraint_audit import auto_transition_issues, explicit_constraint_issues


LOCATOR_ACTIONS = {
    ActionType.TAP,
    ActionType.LONG_PRESS,
    ActionType.INPUT_TEXT,
    ActionType.WAIT_FOR,
    ActionType.ASSERT_VISIBLE,
    ActionType.ASSERT_ENABLED,
    ActionType.ASSERT_PAGE,
}
HIGH_RISK_TERMS = {"支付", "付款", "购买", "发布", "删除账号", "注销账号", "转账"}
READ_ONLY_ACTIONS = {ActionType.WAIT_FOR, ActionType.ASSERT_VISIBLE, ActionType.ASSERT_ENABLED, ActionType.ASSERT_PAGE}


def validate_plan(plan: ExecutionPlan, *, baseline_plan: ExecutionPlan | None = None) -> ValidationResult:
    issues: list[ValidationIssue] = auto_transition_issues(plan)
    if plan.compiler_mode.startswith("ai"):
        issues.extend(explicit_constraint_issues(plan))
    step_ids: set[str] = set()

    if plan.compiler_mode == "rule_seed":
        issues.append(_issue(
            "compiler_mode", "rule_seed_incomplete",
            "规则模板只提供通用草稿，尚未按原始用例核对动作和结果，不能确认或执行",
        ))

    if not plan.steps or plan.steps[0].action != ActionType.LAUNCH_APP:
        issues.append(_issue("steps[0]", "launch_app_required", "可重复执行计划必须从启动 APP 开始"))
    if (plan.compiler_mode.startswith("ai") and "点击" in plan.source.steps
            and not any(step.action == ActionType.TAP for step in plan.steps)):
        issues.append(_issue("steps", "source_tap_missing",
                             "原用例要求点击，但生成计划没有点击动作；需补定位后再执行"))
    if (plan.compiler_mode.startswith("ai")
            and re.search(r"(?:^|[；;。\n]|\d+[.、])\s*点击(?:手机)?桌面(?:行李|出行提醒|今日行程)?(?:小)?组件", plan.source.steps)
            and not any(step.action == ActionType.TAP and (
                step.target_key == "launcher.app1_widget"
                or step.locator is not None
            ) for step in plan.steps)):
        issues.append(_issue("steps", "source_desktop_entry_missing",
                             "原文明确要求点击桌面组件，不能用应用内导航点击替代"))

    for index, step in enumerate(plan.steps):
        path = f"steps[{index}]"
        declared_identity = step.metadata.get("creates_object_identity")
        if step.metadata.get("capture_text_as") and step.action not in READ_ONLY_ACTIONS:
            issues.append(_issue(path, "capture_text_must_be_read_only", "现场对象名称应在只读核查步骤采集"))
        if declared_identity is not None and (not isinstance(declared_identity, str) or not declared_identity.strip()):
            issues.append(_issue(path, "invalid_created_identity", "创建身份须为实际名称或可解析参数，不能用布尔标记代替"))
        if plan.compiler_mode.startswith("ai") and step.metadata.get("blocking_reason"):
            issues.append(_issue(path, "unresolved_execution_requirement", str(step.metadata["blocking_reason"])))
        if step.step_id in step_ids:
            issues.append(_issue(f"{path}.step_id", "duplicate_step_id", "步骤编号不能重复"))
        step_ids.add(step.step_id)

        if step.action in LOCATOR_ACTIONS and step.locator is None and step.target_key is None:
            issues.append(_issue(f"{path}.locator", "locator_required", "该动作必须提供定位目标"))
        if step.target_key and not target_exists(step.target_key):
            issues.append(_issue(f"{path}.target_key", "unknown_target_key", "定位目标不在已验证定位库中"))
        if step.action == ActionType.INPUT_TEXT and not step.value:
            issues.append(_issue(f"{path}.value", "input_value_required", "输入动作必须提供测试数据"))
        named_plans = [str(oracle.expected) for oracle in plan.oracles
                       if oracle.type == OracleType.TEXT_EQUALS and oracle.target_key == "editor.plan_name"]
        if step.metadata.get("saved_widget_match"):
            try:
                saved_widget_locator(step.target_key, step.metadata["saved_widget_match"])
            except (ValueError, TypeError) as exc:
                issues.append(_issue(path, "invalid_saved_widget_match", str(exc)))
        if (plan.compiler_mode.startswith("ai") and named_plans and step.action == ActionType.TAP
                and step.target_key == "saved_widget.preview"
                and (step.metadata.get("saved_widget_match") if isinstance(step.metadata.get("saved_widget_match"), dict)
                     else {}).get("plan_name") not in named_plans
                and step.locator is None):
            issues.append(_issue(path, "ambiguous_saved_widget_target",
                                 "已保存组件预览可能有多条，点击前必须唯一定位本次要检查的组件"))

        if step.locator and step.locator.strategy == LocatorStrategy.NORMALIZED_COORDINATE:
            try:
                x_text, y_text = step.locator.value.split(",", maxsplit=1)
                x, y = float(x_text), float(y_text)
                if not (0 <= x <= 1 and 0 <= y <= 1):
                    raise ValueError
            except ValueError:
                issues.append(
                    _issue(
                        f"{path}.locator.value",
                        "invalid_normalized_coordinate",
                        "归一化坐标必须是 0 到 1 之间的 x,y",
                    )
                )

        searchable = " ".join(filter(None, [step.title, step.value or "", step.locator.value if step.locator else ""]))
        if step.action not in READ_ONLY_ACTIONS and any(term in searchable for term in HIGH_RISK_TERMS):
            issues.append(_issue(path, "high_risk_action", "计划包含未授权的高风险操作，需要人工移除"))

    capture_ids = {
        step.step_id for step in plan.steps
        if isinstance(step.metadata.get("capture_element_count"), dict)
    }

    create_steps = [
        *plan.steps,
        *(step for requirement in plan.state_contract.requires for step in requirement.prepare_steps),
    ]
    if (plan.compiler_mode.startswith("ai")
            and ("不重复创建" in plan.source.expected or "仅保存一次" in plan.source.expected)
            and any(step.action == ActionType.TAP and "save_button" in (step.target_key or "")
                    for step in plan.steps)
            and not any(step.metadata.get("creates_object_identity") for step in create_steps)
            and not any(oracle.type == OracleType.ELEMENT_COUNT_DELTA and oracle.saved_widget_match
                        for oracle in plan.oracles)):
        issues.append(_issue("state_contract", "created_object_tracking_required",
                             "验证仅创建一次时，必须记录本轮创建的对象，不能只数历史列表"))
    created_identities = [
        step.metadata.get("creates_object_identity").strip()
        for step in create_steps
        if isinstance(step.metadata.get("creates_object_identity"), str)
        and step.metadata.get("creates_object_identity").strip()
    ]
    if len(created_identities) != len(set(created_identities)):
        keys = [step.metadata.get("created_object_key") for step in create_steps
                if isinstance(step.metadata.get("creates_object_identity"), str)
                and step.metadata.get("creates_object_identity").strip()]
        if any(not isinstance(key, str) or not key.strip() for key in keys) or len(keys) != len(set(keys)):
            issues.append(_issue(
                "state_contract", "created_object_key_required",
                "允许创建同名对象，但每次创建必须有不同的对象键，后续才能准确映射本次对象",
            ))
    if plan.compiler_mode.startswith("ai"):
        created = [step for step in create_steps if step.metadata.get("creates_object_identity")]
        keys = [step.metadata.get("created_object_key") for step in created]
        if any(not isinstance(key, str) or not key.strip() for key in keys) or len(keys) != len(set(keys)):
            issues.append(_issue("state_contract", "created_object_key_required",
                                 "每次创建必须声明不同的对象键，允许按测试需要创建同名对象"))
        for create_step in created:
            key = create_step.metadata.get("created_object_key")
            if not isinstance(key, str):
                continue
            if baseline_plan is not None and not baseline_plan.compiler_mode.startswith("ai"):
                original = next((step for step in baseline_plan.steps if step.step_id == create_step.step_id), None)
                if original is not None and original == create_step:
                    continue
            create_group = plan.steps if create_step in plan.steps else next(
                (requirement.prepare_steps for requirement in plan.state_contract.requires
                 if create_step in requirement.prepare_steps), []
            )
            later_steps = create_group[create_group.index(create_step) + 1:]
            if not any(step.action in READ_ONLY_ACTIONS
                       and step.metadata.get("verifies_created_object_key") == key
                       for step in later_steps):
                issues.append(_issue("state_contract", "created_object_verification_required",
                                     f"本次创建的对象 {key} 必须在创建后用只读步骤核实唯一身份"))
    for index, step in enumerate(plan.steps):
        capture = step.metadata.get("capture_element_count")
        if capture is None:
            continue
        path = f"steps[{index}].metadata.capture_element_count"
        if step.action not in READ_ONLY_ACTIONS:
            issues.append(_issue(path, "count_capture_must_be_read_only", "数量基线必须在只读核查步骤中采集"))
        if not isinstance(capture, dict):
            issues.append(_issue(path, "count_capture_target_required", "数量基线必须提供 locator 或 target_key"))
            continue
        locator_data = capture.get("locator") or (capture if "strategy" in capture else None)
        target_key = capture.get("target_key")
        if capture.get("saved_widget_match"):
            try:
                saved_widget_locator(target_key, capture["saved_widget_match"])
            except (ValueError, TypeError) as exc:
                issues.append(_issue(path, "invalid_saved_widget_match", str(exc)))
        try:
            if locator_data is not None:
                Locator.model_validate(locator_data)
                locator_valid = True
            else:
                locator_valid = False
        except Exception:
            locator_valid = False
        if not locator_valid and not target_key:
            issues.append(_issue(path, "count_capture_target_required", "数量基线必须提供有效 locator 或 target_key"))
        if target_key and not target_exists(target_key):
            issues.append(_issue(f"{path}.target_key", "unknown_target_key", "数量基线目标不在已验证定位库中"))

    contract = plan.state_contract
    if contract.mode != StateMode.LEGACY_UNMANAGED:
        if contract.mode == StateMode.DESTRUCTIVE and not contract.mutates:
            issues.append(_issue("state_contract.mutates", "mutations_required", "破坏性用例必须声明会修改的数据域"))
        if len(contract.mutates) != len(set(contract.mutates)):
            issues.append(_issue("state_contract.mutates", "duplicate_domain", "受影响的数据域不能重复"))
        domains = [requirement.domain for requirement in contract.requires]
        if len(domains) != len(set(domains)):
            issues.append(_issue("state_contract.requires", "duplicate_domain", "前置条件的数据域不能重复"))
        for requirement_index, requirement in enumerate(contract.requires):
            if not requirement.check_oracles:
                issues.append(_issue(
                    f"state_contract.requires[{requirement_index}].check_oracles",
                    "state_check_required",
                    "每个受控前置条件必须提供至少一个核查验收项",
                ))
            _validate_fragment(requirement.check_steps, requirement.check_oracles, f"state_contract.requires[{requirement_index}]", issues, require_oracles=False)
            if requirement.prepare_steps:
                _validate_fragment(requirement.prepare_steps, requirement.prepare_oracles, f"state_contract.requires[{requirement_index}].prepare", issues, require_oracles=True)
        if contract.restore_steps:
            _validate_fragment(contract.restore_steps, contract.restore_oracles, "state_contract.restore", issues, require_oracles=True)
            if contract.mode == StateMode.DESTRUCTIVE:
                keyed_created = {
                    step.metadata.get("created_object_key"): step.metadata.get("creates_object_identity")
                    for step in create_steps if step.metadata.get("created_object_key")
                    and step.metadata.get("creates_object_identity")
                }
                if keyed_created:
                    keyed_restored = {step.metadata.get("owns_object_key") for step in contract.restore_steps
                                      if step.metadata.get("owns_object_key")}
                    if keyed_restored != set(keyed_created):
                        issues.append(_issue(
                            "state_contract.restore_steps", "restore_ownership_required",
                            "每个本轮对象键都需要对应的精确恢复步骤，不能只按同名文本合并",
                        ))
                    for step in contract.restore_steps:
                        key = step.metadata.get("owns_object_key")
                        if key in keyed_created and (
                            step.metadata.get("owns_object_identity") != keyed_created[key]
                            or step.metadata.get("owned_object_expected_text") != keyed_created[key]
                            or not step.metadata.get("owned_object_target_key")
                        ):
                            issues.append(_issue(
                                "state_contract.restore_steps", "restore_identity_check_required",
                                f"恢复对象键 {key} 必须核对其本轮名称和唯一定位目标",
                            ))
                created = {
                    step.metadata.get("creates_object_identity")
                    for step in create_steps
                    if isinstance(step.metadata.get("creates_object_identity"), str)
                    and step.metadata.get("creates_object_identity").strip()
                }
                cleanup = {
                    step.metadata.get("owns_object_identity")
                    for step in contract.restore_steps
                    if isinstance(step.metadata.get("owns_object_identity"), str)
                    and step.metadata.get("owns_object_identity").strip()
                }
                if not created or cleanup != created:
                    issues.append(_issue(
                        "state_contract.restore_steps", "restore_ownership_required",
                        "破坏性恢复必须逐对象匹配本轮创建身份；不能清理未证明归本轮所有的数据",
                    ))
                for identity in cleanup:
                    has_identity_check = any(
                        step.metadata.get("owns_object_identity") == identity
                        and step.metadata.get("owned_object_target_key")
                        and step.metadata.get("owned_object_expected_text") == identity
                        for step in contract.restore_steps
                    )
                    if not has_identity_check:
                        issues.append(_issue(
                            "state_contract.restore_steps", "restore_identity_check_required",
                            f"恢复对象 {identity} 必须在操作前声明唯一定位目标和精确身份文本",
                        ))

    oracle_ids: set[str] = set()
    for index, oracle in enumerate(plan.oracles):
        path = f"oracles[{index}]"
        if oracle.scroll_container and (oracle.type not in {OracleType.VISIBLE, OracleType.TEXT_EQUALS}
                                        or oracle.saved_widget_match):
            issues.append(_issue(path, "unsupported_scroll_search", "列表查找只支持 visible/text_equals，不能与组件专用查找混用"))
        if oracle.reference_step_id and oracle.type != OracleType.ELEMENT_COUNT_DELTA:
            issues.append(_issue(path, "unsupported_oracle_reference",
                                 "验收在主步骤结束后执行；reference_step_id 仅用于数量差，不能回看先前页面"))
        if oracle.saved_widget_match and oracle.type not in {OracleType.ELEMENT_COUNT_DELTA, OracleType.ELEMENT_COUNT,
                                                            OracleType.VISIBLE, OracleType.NOT_VISIBLE}:
            issues.append(_issue(path, "unsupported_scoped_oracle", "此验收类型不支持组件范围筛选"))
        if plan.compiler_mode.startswith("ai"):
            source_text = " ".join((plan.source.title, plan.source.precondition,
                                    plan.source.steps, plan.source.expected))
            basis = (oracle.source_basis or "").strip()
            inherited_acceptance = (
                plan.compiler_mode == "ai_review_repair" and baseline_plan is not None
                and plan.source == baseline_plan.source and oracle in baseline_plan.oracles
            )
            if not inherited_acceptance and (not basis or oracle.basis_kind is None):
                issues.append(_issue(f"{path}.source_basis", "oracle_basis_required",
                                     "AI 生成的硬验收必须说明来自原用例，或为何是最小业务结果"))
            elif not inherited_acceptance and oracle.basis_kind == "explicit" and basis not in source_text:
                issues.append(_issue(f"{path}.source_basis", "oracle_basis_not_in_source",
                                     "声明为原文明确要求的依据必须能在原用例中找到"))
            hard_text = f"{oracle.title} {oracle.expected if oracle.expected is not None else ''}"
            size_terms = {
                "宽版": ("宽版", "宽屏", "大号", "大尺寸"),
                "紧凑版": ("紧凑版", "小号", "小尺寸"),
                "大号": ("宽版", "大号", "大尺寸"),
                "小号": ("紧凑版", "小号", "小尺寸"),
                "中号": ("中号", "中尺寸"),
            }
            for term, source_aliases in size_terms.items():
                if term in hard_text and not any(alias in source_text for alias in source_aliases):
                    issues.append(_issue(path, "unsupported_size_condition",
                                         "原用例未要求该组件尺寸，不能加入硬验收"))
                    break
            if oracle.type == OracleType.TEXT_SEQUENCE and not any(
                term in source_text for term in ("顺序", "依次", "排序", "先后")
            ):
                issues.append(_issue(path, "unsupported_sequence_condition",
                                     "原用例未要求顺序，不能加入文本顺序硬验收"))
            amounts = re.findall(r"\d+(?:\.\d+)?\s*元", hard_text)
            if any(amount.replace(" ", "") not in source_text.replace(" ", "") for amount in amounts):
                issues.append(_issue(path, "unsupported_exact_amount",
                                     "原用例未指定该金额，不能加入精确金额硬验收"))
            pages = re.findall(r"第[一二三四五六七八九十\d]+(?:个)?(?:桌面)?页", hard_text)
            if any(page not in source_text for page in pages):
                issues.append(_issue(path, "unsupported_page_position",
                                     "原用例未指定该页位置，不能加入固定页码硬验收"))
        if oracle.oracle_id in oracle_ids:
            issues.append(_issue(f"{path}.oracle_id", "duplicate_oracle_id", "验收规则编号不能重复"))
        oracle_ids.add(oracle.oracle_id)
        if oracle.type in {OracleType.VISIBLE, OracleType.NOT_VISIBLE, OracleType.ENABLED, OracleType.DISABLED, OracleType.PAGE, OracleType.TEXT_EQUALS, OracleType.TEXT_SEQUENCE, OracleType.TEXT_CONTAINS_ALL, OracleType.DATE_COUNTDOWN, OracleType.ELEMENT_COUNT, OracleType.ELEMENT_COUNT_DELTA} and oracle.locator is None and oracle.target_key is None:
            issues.append(_issue(f"{path}.locator", "oracle_locator_required", "页面验收规则必须提供定位目标"))
        if oracle.target_key and not target_exists(oracle.target_key):
            issues.append(_issue(f"{path}.target_key", "unknown_target_key", "验收目标不在已验证定位库中"))
        if oracle.type == OracleType.TEXT_EQUALS and oracle.expected is None:
            issues.append(_issue(f"{path}.expected", "oracle_expected_required", "文本相等验收必须提供期望值"))
        if oracle.type == OracleType.TEXT_SEQUENCE and not isinstance(oracle.expected, list):
            issues.append(_issue(f"{path}.expected", "oracle_sequence_required", "文本顺序验收必须提供期望文本列表"))
        if oracle.type == OracleType.TEXT_CONTAINS_ALL and not isinstance(oracle.expected, list):
            issues.append(_issue(f"{path}.expected", "oracle_contains_required", "文本包含验收必须提供期望文本列表"))
        if oracle.type == OracleType.DATE_COUNTDOWN:
            try:
                date.fromisoformat(str(oracle.expected))
            except ValueError:
                issues.append(_issue(f"{path}.expected", "oracle_date_required", "动态倒计时验收必须提供 YYYY-MM-DD 日期"))
        if oracle.type == OracleType.ELEMENT_COUNT and (
            not isinstance(oracle.expected, int) or isinstance(oracle.expected, bool) or oracle.expected < 0
        ):
            issues.append(_issue(f"{path}.expected", "oracle_count_required", "元素数量验收必须提供大于或等于 0 的整数"))
        if oracle.type == OracleType.ELEMENT_COUNT_DELTA:
            if oracle.saved_widget_match:
                try:
                    saved_widget_locator(oracle.target_key, oracle.saved_widget_match)
                except (ValueError, TypeError) as exc:
                    issues.append(_issue(path, "invalid_saved_widget_match", str(exc)))
                baseline = next((step.metadata.get("capture_element_count", {}) for step in plan.steps
                                 if step.step_id == oracle.reference_step_id), {})
                if baseline.get("saved_widget_match") != oracle.saved_widget_match or baseline.get("visible_only"):
                    issues.append(_issue(path, "count_scope_mismatch",
                                         "创建前后必须核对同一对象范围的完整列表数量"))
            if not isinstance(oracle.expected, int) or isinstance(oracle.expected, bool):
                issues.append(_issue(f"{path}.expected", "oracle_count_delta_required", "数量变化验收必须提供整数变化量"))
            if not oracle.reference_step_id or oracle.reference_step_id not in capture_ids:
                issues.append(_issue(f"{path}.reference_step_id", "oracle_count_baseline_required",
                                     "数量变化验收必须引用一个记录过基线数量的步骤"))
            elif next((i for i, step in enumerate(plan.steps) if step.step_id == oracle.reference_step_id), 10**9) >= len(plan.steps) - 1:
                issues.append(_issue(f"{path}.reference_step_id", "oracle_count_baseline_order",
                                     "数量基线步骤必须早于执行结束"))
        if oracle.type == OracleType.VISUAL_ASSERTION and not isinstance(oracle.expected, str):
            issues.append(_issue(f"{path}.expected", "visual_expectation_required", "视觉验收必须提供明确的预期画面描述"))

    return ValidationResult(valid=not issues, issues=issues)


def _validate_fragment(
    steps: list[PlanStep],
    oracles: list,
    path: str,
    issues: list[ValidationIssue],
    *,
    require_oracles: bool,
) -> None:
    if not steps:
        issues.append(_issue(f"{path}.check_steps", "state_steps_required", "状态片段必须提供步骤"))
    if require_oracles and not oracles:
        issues.append(_issue(f"{path}.check_oracles", "state_oracles_required", "状态片段必须提供验收项"))
    for index, step in enumerate(steps):
        if step.target_key and not target_exists(step.target_key):
            issues.append(_issue(f"{path}.steps[{index}].target_key", "unknown_target_key", "定位目标不在已验证定位库中"))
    for index, oracle in enumerate(oracles):
        if oracle.target_key and not target_exists(oracle.target_key):
            issues.append(_issue(f"{path}.oracles[{index}].target_key", "unknown_target_key", "验收目标不在已验证定位库中"))


def _issue(path: str, code: str, message: str) -> ValidationIssue:
    return ValidationIssue(path=path, code=code, message=message)
