from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .model_config import (
    ModelEndpointConfig,
    load_plan_gateway_config,
    save_selected_plan_model,
)


EXCLUDED_MODEL_MARKERS = (
    "audio",
    "realtime",
    "image",
    "embedding",
    "rerank",
    "tts",
    "speech",
    "transcribe",
)
PREFERRED_PLAN_MODELS = (
    "gpt-5.5",
    "gpt-5.6-sol",
    "gpt-5.6-luna",
    "gpt-5.4-mini",
    "gpt-5.2-chat-latest",
    "gpt-5.2",
)

# A 64x64 PNG with a blue square, used only to verify multimodal input. Some
# providers reject 1x1 images even when the PNG itself is valid.
VISION_PROBE_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAAAXNSR0IArs4c6QAAAARnQU1BAACxjwv8YQUAAAAJcEhZcwAADsMAAA7DAcdvqGQAAADTSURBVHhe7ZCxCQMBEMN+tYyTsTNJ0qszhkthCdyoMnq+4zwUaxiAYg0DUKxhAIo1DECxhgEo1jAAxRoGoFijDvB6f/66FgNQpPDQ9VoMQJHCQ9drMQBFCg9dr8UAFCk8dL0WA1Ck8ND1WgxAkcJD12sxAEUKD12vxQAUKTx0vRYDUKTw0PVaDECRwkPXazEARQoPXa/FABQpPHS9FgNQpPDQ9VoMQJHCQ9drMQBFCg9dr8UAFGsYgGINA1CsYQCKNQxAsYYBKNYwAMUaBqBYYz7ADwAJ+1iZuUwEAAAAAElFTkSuQmCC"
)


@dataclass(frozen=True)
class ModelProbeResult:
    model: str
    passed: bool
    latency_ms: int
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "passed": self.passed,
            "latency_ms": self.latency_ms,
            "message": self.message,
        }


def discover_models(config: ModelEndpointConfig) -> list[str]:
    response = httpx.get(
        f"{config.base_url}/models",
        headers={"Authorization": f"Bearer {config.api_key}"},
        timeout=20,
    )
    response.raise_for_status()
    entries = response.json().get("data", [])
    return sorted(
        {
            str(entry.get("id") or "").strip()
            for entry in entries
            if isinstance(entry, dict) and entry.get("id")
        }
    )


def discover_plan_models(config: ModelEndpointConfig | None = None) -> list[str]:
    gateway = config or load_plan_gateway_config()
    if gateway is None:
        raise RuntimeError("尚未配置用例解析模型中转站")
    model_ids = discover_models(gateway)
    candidates = [
        model_id
        for model_id in model_ids
        if not any(marker in model_id.lower() for marker in EXCLUDED_MODEL_MARKERS)
        and model_id != "codex-auto-review"
    ]
    preferred_index = {name: index for index, name in enumerate(PREFERRED_PLAN_MODELS)}
    return sorted(candidates, key=lambda name: (preferred_index.get(name, 999), name))


def probe_plan_model(model: str, config: ModelEndpointConfig | None = None) -> ModelProbeResult:
    gateway = config or load_plan_gateway_config()
    if gateway is None:
        raise RuntimeError("尚未配置用例解析模型中转站")
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "你是测试用例结构化探针。只返回 JSON，不输出解释。",
            },
            {
                "role": "user",
                "content": (
                    "把‘启动APP，点击我的，确认进入个人页’整理为JSON。"
                    "必须包含 intent 字符串、steps 数组、oracles 数组；"
                    "steps 每项必须包含 action，oracles 每项必须包含 type。"
                ),
            },
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    started = time.monotonic()
    try:
        response = httpx.post(
            f"{gateway.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {gateway.api_key}"},
            json=payload,
            timeout=60,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        result = json.loads(content)
        valid = (
            isinstance(result.get("intent"), str)
            and isinstance(result.get("steps"), list)
            and bool(result["steps"])
            and all(isinstance(item, dict) and item.get("action") for item in result["steps"])
            and isinstance(result.get("oracles"), list)
            and bool(result["oracles"])
            and all(isinstance(item, dict) and item.get("type") for item in result["oracles"])
        )
        message = "结构化 JSON 探针通过" if valid else "返回 JSON 未满足测试计划基本字段"
        return ModelProbeResult(model, valid, round((time.monotonic() - started) * 1000), message)
    except Exception as exc:
        return ModelProbeResult(
            model,
            False,
            round((time.monotonic() - started) * 1000),
            f"{type(exc).__name__}: {str(exc)[:240]}",
        )


def probe_vision_model(model: str, config: ModelEndpointConfig) -> ModelProbeResult:
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "你是视觉接口能力探针。只返回 JSON。",
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": '确认是否收到图片，返回 {"image_received":true}。'},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{VISION_PROBE_PNG}"},
                    },
                ],
            },
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    started = time.monotonic()
    try:
        response = httpx.post(
            f"{config.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {config.api_key}"},
            json=payload,
            timeout=60,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        result = json.loads(content)
        passed = result.get("image_received") is True
        return ModelProbeResult(
            model,
            passed,
            round((time.monotonic() - started) * 1000),
            "图片输入探针通过" if passed else "接口响应未确认图片输入",
        )
    except Exception as exc:
        return ModelProbeResult(
            model,
            False,
            round((time.monotonic() - started) * 1000),
            f"{type(exc).__name__}: {str(exc)[:240]}",
        )


def auto_select_plan_model(
    *,
    max_candidates: int = 5,
    selection_file: Path | None = None,
) -> tuple[str | None, list[ModelProbeResult], list[str]]:
    candidates = discover_plan_models()
    probes: list[ModelProbeResult] = []
    for model in candidates[:max_candidates]:
        probe = probe_plan_model(model)
        probes.append(probe)
        if probe.passed:
            save_selected_plan_model(model, selection_file)
            return model, probes, candidates
    return None, probes, candidates
