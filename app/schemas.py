from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class PlanStatus(StrEnum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"
    VALIDATED = "validated"
    CANDIDATE = "candidate"


class DevicePlatform(StrEnum):
    ANDROID = "android"
    IOS = "ios"


class ActionType(StrEnum):
    LAUNCH_APP = "launch_app"
    TAP = "tap"
    LONG_PRESS = "long_press"
    INPUT_TEXT = "input_text"
    SWIPE = "swipe"
    BACK = "back"
    BACKGROUND_APP = "background_app"
    GO_HOME = "go_home"
    LOCK_SCREEN = "lock_screen"
    UNLOCK_SCREEN = "unlock_screen"
    TERMINATE_APP = "terminate_app"
    WAIT_FOR = "wait_for"
    ASSERT_VISIBLE = "assert_visible"
    ASSERT_ENABLED = "assert_enabled"
    ASSERT_PAGE = "assert_page"


class LocatorStrategy(StrEnum):
    RESOURCE_ID = "resource_id"
    ACCESSIBILITY_ID = "accessibility_id"
    TEXT = "text"
    ANDROID_UIAUTOMATOR = "android_uiautomator"
    XPATH = "xpath"
    IOS_PREDICATE = "ios_predicate"
    IOS_CLASS_CHAIN = "ios_class_chain"
    NORMALIZED_COORDINATE = "normalized_coordinate"


class OracleType(StrEnum):
    VISIBLE = "visible"
    NOT_VISIBLE = "not_visible"
    ENABLED = "enabled"
    DISABLED = "disabled"
    PAGE = "page"
    TEXT_EQUALS = "text_equals"
    TEXT_SEQUENCE = "text_sequence"
    TEXT_CONTAINS_ALL = "text_contains_all"
    DATE_COUNTDOWN = "date_countdown"
    ELEMENT_COUNT = "element_count"
    ELEMENT_COUNT_DELTA = "element_count_delta"
    VISUAL_ASSERTION = "visual_assertion"


class StateMode(StrEnum):
    LEGACY_UNMANAGED = "legacy_unmanaged"
    READ_ONLY = "read_only"
    IDEMPOTENT_WRITE = "idempotent_write"
    DESTRUCTIVE = "destructive"


class RestorePolicy(StrEnum):
    NONE = "none"
    ON_SUCCESS = "on_success"
    ALWAYS = "always"


class Locator(BaseModel):
    strategy: LocatorStrategy
    value: str = Field(min_length=1, max_length=500)


class Oracle(BaseModel):
    oracle_id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=200)
    type: OracleType
    target_key: str | None = Field(default=None, min_length=1, max_length=120)
    locator: Locator | None = None
    expected: str | bool | int | list[str] | None = None
    reference_step_id: str | None = Field(default=None, min_length=1, max_length=80)
    timeout_seconds: float = Field(default=5, ge=0, le=60)
    source_basis: str | None = Field(default=None, max_length=300)
    basis_kind: Literal["explicit", "business_minimum"] | None = None
    saved_widget_match: dict[str, str] = Field(default_factory=dict)
    scroll_container: Locator | None = Field(default=None, description="已核实的 Android 滚动列表；仅用于 visible/text_equals 查找，不改变验收目标")


class PlanStep(BaseModel):
    step_id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=200)
    action: ActionType
    target_key: str | None = Field(default=None, min_length=1, max_length=120)
    locator: Locator | None = None
    value: str | None = Field(default=None, max_length=2000)
    timeout_seconds: float = Field(default=8, ge=0.2, le=60)
    allow_ai_repair: bool = False
    postcondition_policy: Literal["required", "advisory"] = "required"
    metadata: dict[str, Any] = Field(default_factory=dict)


class StateRequirement(BaseModel):
    domain: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=300)
    check_steps: list[PlanStep] = Field(default_factory=list, max_length=30)
    check_oracles: list[Oracle] = Field(default_factory=list, max_length=20)
    prepare_steps: list[PlanStep] = Field(default_factory=list, max_length=30)
    prepare_oracles: list[Oracle] = Field(default_factory=list, max_length=20)


class StateContract(BaseModel):
    mode: StateMode = StateMode.LEGACY_UNMANAGED
    requires: list[StateRequirement] = Field(default_factory=list, max_length=20)
    mutates: list[str] = Field(default_factory=list, max_length=20)
    restore: RestorePolicy = RestorePolicy.NONE
    restore_steps: list[PlanStep] = Field(default_factory=list, max_length=30)
    restore_oracles: list[Oracle] = Field(default_factory=list, max_length=20)


class TestParameterRule(BaseModel):
    source: Literal["fixed", "today", "run_id"]
    value: str | None = Field(default=None, max_length=200)
    offset_days: int = Field(default=0, ge=-3650, le=3650)
    date_format: Literal["%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d"] = "%Y-%m-%d"

    @model_validator(mode="after")
    def validate_source(self):
        if self.source == "fixed" and not self.value:
            raise ValueError("固定测试参数必须提供 value")
        return self


class SourceCase(BaseModel):
    case_id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=300)
    precondition: str = ""
    steps: str = ""
    expected: str = ""
    priority: str = ""
    case_type: str = ""
    source_sheet: str = ""
    source_row: int = Field(ge=1)


class ExecutionPlan(BaseModel):
    schema_version: str = "1.0"
    plan_id: str = Field(min_length=1, max_length=120)
    version: int = Field(default=1, ge=1)
    status: PlanStatus = PlanStatus.DRAFT
    source: SourceCase
    intent: str = Field(min_length=1, max_length=1000)
    preconditions: list[str] = Field(default_factory=list)
    state_contract: StateContract = Field(default_factory=StateContract)
    locator_snapshot: dict[str, list[Locator]] = Field(default_factory=dict)
    test_parameters: dict[str, TestParameterRule] = Field(default_factory=dict)
    steps: list[PlanStep] = Field(min_length=1, max_length=50)
    oracles: list[Oracle] = Field(min_length=1, max_length=20)
    compiler_mode: str = Field(default="manual", max_length=80)
    compiler_model: str | None = Field(default=None, max_length=120)
    qa_confirmed_by: str | None = Field(default=None, max_length=100)
    qa_confirmed_at: str | None = None


class ValidationIssue(BaseModel):
    path: str
    code: str
    message: str


class ValidationResult(BaseModel):
    valid: bool
    scope: str = "static"
    issues: list[ValidationIssue] = Field(default_factory=list)


class PlanRecord(BaseModel):
    record_id: int
    plan: ExecutionPlan
    validation: ValidationResult
    created_at: str
    updated_at: str
