"""One bounded AI proposal; the collection worker validates and performs actions."""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .model_config import load_plan_model_config, load_vision_model_config


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CollectedControl(StrictModel):
    node_id: str
    target_key: str = Field(pattern=r"^[a-z][a-z0-9_.]{1,119}$")
    description: str = Field(min_length=1, max_length=200)


class PreparationObservation(StrictModel):
    case_id: str
    condition: str = Field(min_length=1, max_length=300)
    source_basis: str = Field(min_length=1, max_length=500)
    status: Literal["observed", "external", "unknown"]
    node_ids: list[str] = Field(default_factory=list, max_length=12)
    expected_text: str = Field(default="", max_length=200)
    reason: str = Field(min_length=1, max_length=500)


class CollectionAction(StrictModel):
    type: Literal["tap", "input", "swipe", "back", "wait", "stop"]
    effect: Literal["navigate", "create", "delete", "purchase", "logout", "cancel_account", "share"] = "navigate"
    node_id: str | None = None
    visual_hint: str = Field(default="", max_length=200)
    value: str = Field(default="", max_length=500)
    direction: Literal["up", "down", "left", "right"] = "up"
    seconds: float = Field(default=1, ge=0.2, le=5)
    expected_text: str = Field(default="", max_length=200)
    object_name: str = Field(default="", max_length=200)
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def target_required(self):
        if self.type in {"tap", "input"} and not (self.node_id or self.visual_hint):
            raise ValueError("点击/输入必须引用当前节点或提供视觉目标")
        return self


class CollectionDecision(StrictModel):
    snapshot_id: str
    page_name: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=1000)
    controls: list[CollectedControl] = Field(default_factory=list, max_length=60)
    observations: list[PreparationObservation] = Field(default_factory=list, max_length=50)
    action: CollectionAction
    missing: list[str] = Field(default_factory=list, max_length=30)


class CollectionAI:
    def __init__(self):
        self.text = load_plan_model_config()
        self.vision = load_vision_model_config()
        if not self.text and not self.vision:
            raise RuntimeError("信息采集需要先配置解析模型或视觉模型")

    def decide(self, context: dict, screenshot: Path | None, *, timeout: float = 30) -> CollectionDecision:
        config = self.vision if screenshot and self.vision else self.text or self.vision
        prompt = Path(__file__).with_name("prompts").joinpath("collection.md").read_text(encoding="utf-8")
        text = json.dumps({**context, "output_schema": CollectionDecision.model_json_schema()}, ensure_ascii=False)
        content = [{"type": "text", "text": text}]
        if screenshot and config == self.vision:
            if screenshot.stat().st_size > 5_000_000:
                raise ValueError("截图过大，未发送给模型")
            encoded = base64.b64encode(screenshot.read_bytes()).decode("ascii")
            content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}})
        messages = [{"role": "system", "content": prompt}, {"role": "user", "content": content}]
        # Two format attempts share the caller's time budget; neither performs a phone action.
        import time
        deadline = time.monotonic() + timeout
        for attempt in range(2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("采集模型请求超时，未执行手机动作")
            response = httpx.post(config.base_url.rstrip("/") + "/chat/completions",
                                  headers={"Authorization": f"Bearer {config.api_key}"},
                                  json={"model": config.model, "messages": messages, "temperature": 0,
                                        "response_format": {"type": "json_object"}}, timeout=remaining)
            response.raise_for_status()
            raw = response.json()["choices"][0]["message"]["content"]
            try:
                return CollectionDecision.model_validate_json(raw)
            except ValueError as exc:
                if attempt:
                    raise ValueError("采集模型输出不符合结构，未执行动作") from exc
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content": "只纠正输出格式，返回符合 output_schema 的 JSON；不得新增动作。"})
        raise AssertionError("unreachable")
