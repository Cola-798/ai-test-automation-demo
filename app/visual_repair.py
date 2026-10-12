from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
from pydantic import BaseModel, Field, ValidationError

from .model_config import load_float, load_vision_model_config
from .schemas import PlanStep


class VisualProposal(BaseModel):
    visible: bool
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=500)


class VisualAssertion(BaseModel):
    passed: bool
    confidence: float = Field(ge=0, le=1)
    observed_text: str = Field(default="", max_length=500)
    reason: str = Field(min_length=1, max_length=500)


class VisualRepairService:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        minimum_confidence: float = 0.85,
        timeout_seconds: float = 90,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.minimum_confidence = minimum_confidence
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_environment(cls) -> "VisualRepairService | None":
        config = load_vision_model_config()
        if config is None:
            return None
        return cls(
            base_url=config.base_url,
            api_key=config.api_key,
            model=config.model,
            minimum_confidence=load_float(
                "VISION_MODEL_MIN_CONFIDENCE", 0.85, minimum=0.0, maximum=1.0
            ),
            timeout_seconds=load_float(
                "VISION_MODEL_TIMEOUT_SECONDS", 90.0, minimum=1.0, maximum=300.0
            ),
        )

    def propose(self, step: PlanStep, screenshot: Path) -> VisualProposal:
        encoded = base64.b64encode(screenshot.read_bytes()).decode("ascii")
        hint = str(step.metadata.get("visual_hint") or step.title)
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": "你是移动端测试视觉定位器。只返回 JSON，不执行操作。坐标必须是相对整张截图宽高的 0~1 小数。无法确认目标时 visible=false，禁止猜测。",
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"在截图中定位测试步骤目标：{hint}。返回 "
                                '{"visible":true,"x":0.5,"y":0.5,"confidence":0.9,"reason":"..."}'
                            ),
                        },
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
                    ],
                },
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        return self._request(payload, VisualProposal)

    def accepted(self, proposal: VisualProposal) -> bool:
        return proposal.visible and proposal.confidence >= self.minimum_confidence

    def verify(self, screenshot: Path, *, requirement: str,
               history: list[tuple[Path, dict]] | None = None) -> VisualAssertion:
        encoded = base64.b64encode(screenshot.read_bytes()).decode("ascii")
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是移动端测试视觉验收器。只根据截图判断，不执行操作，不补全看不清的内容。"
                        "历史截图按本轮执行时间排列，仅用于前后变化和对象核对；最终状态必须以标注的当前截图为准。"
                        "历史成功画面不能冒充当前成功，缺少对象对应关系时不得假定是同一条数据。"
                        "只返回 JSON；证据不足时 passed=false 且 confidence 不得高于 0.6。"
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"验收要求：{requirement}。返回 "
                                '{"passed":true,"confidence":0.95,"observed_text":"截图中实际看到的关键文字",'
                                '"reason":"判断依据"}'
                            ),
                        },
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
                    ],
                },
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        content = payload["messages"][1]["content"]
        previous = []
        for path, event in history or []:
            encoded_history = base64.b64encode(path.read_bytes()).decode("ascii")
            mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
            previous.extend([
                {"type": "text", "text": "本轮历史截图：" + json.dumps(event, ensure_ascii=False)},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded_history}"}},
            ])
        content[1:1] = [*previous, {"type": "text", "text": "当前截图（最终状态）："}]
        return self._request(payload, VisualAssertion)

    def _request(self, payload: dict, response_model):
        for attempt in range(2):
            response = httpx.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            try:
                content = response.json()["choices"][0]["message"]["content"]
                if isinstance(content, list):
                    content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
                if not isinstance(content, str):
                    raise ValueError("视觉模型未返回文本 JSON")
                content = content.strip().removeprefix("```json").removesuffix("```").strip()
                return response_model.model_validate(json.loads(content))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError, ValidationError) as exc:
                if attempt:
                    raise ValueError(f"视觉模型连续两次未返回有效 JSON 结果：{type(exc).__name__}: {exc}") from exc
                payload = {**payload, "messages": [*payload.get("messages", []), {
                    "role": "user", "content": (
                        f"上次结果校验失败：{type(exc).__name__}: {str(exc)[:1000]}。"
                        "请重新根据同一截图返回符合格式的 JSON。定位 x、y 必须为相对整张截图的 0~1 小数，"
                        "不能返回像素坐标；禁止猜测。"
                        + ("无法确认目标时 visible=false。" if response_model is VisualProposal
                           else "验收证据不足时 passed=false。")
                    ),
                }]}

    def assertion_is_conclusive(self, assertion: VisualAssertion) -> bool:
        return assertion.confidence >= self.minimum_confidence
