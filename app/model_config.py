from __future__ import annotations

import os
import json
from dataclasses import dataclass
from pathlib import Path

from .model_settings import load_saved_settings
from .runtime_paths import runtime_data_dir


DEFAULT_TESTAI_BASE_URL = "https://api.example.com/v1"
@dataclass(frozen=True)
class ModelEndpointConfig:
    base_url: str
    api_key: str
    model: str
    source: str

    def public_status(self) -> dict[str, str | bool]:
        return {
            "configured": True,
            "base_url": self.base_url,
            "model": self.model,
            "api_key_configured": bool(self.api_key),
            "source": self.source,
        }


def _load_endpoint(
    groups: tuple[tuple[str, str, str, str], ...],
) -> ModelEndpointConfig | None:
    """Choose the first complete group; never mix credentials from different providers."""
    for source, base_url_name, api_key_name, model_name in groups:
        base_url = os.getenv(base_url_name, "").strip().rstrip("/")
        api_key = os.getenv(api_key_name, "").strip()
        model = os.getenv(model_name, "").strip()
        if all((base_url, api_key, model)):
            return ModelEndpointConfig(
                base_url=base_url,
                api_key=api_key,
                model=model,
                source=source,
            )
    return None


def load_plan_model_config() -> ModelEndpointConfig | None:
    """Load the text model used before runtime to compile test cases into plans."""
    gateway = load_plan_gateway_config()
    if gateway is None:
        return None
    model = (
        gateway.model
        or os.getenv("PLAN_MODEL_NAME", "").strip()
        or os.getenv("AI_PLAN_MODEL", "").strip()
        or load_selected_plan_model()
    )
    if not model:
        return None
    return ModelEndpointConfig(
        base_url=gateway.base_url,
        api_key=gateway.api_key,
        model=model,
        source=gateway.source,
    )


def load_plan_gateway_config() -> ModelEndpointConfig | None:
    saved = load_saved_settings("plan")
    if saved and saved["base_url"] and saved["api_key"]:
        return ModelEndpointConfig(
            saved["base_url"], saved["api_key"], saved["model"], "workbench"
        )
    base_url = os.getenv("PLAN_MODEL_BASE_URL", "").strip().rstrip("/")
    api_key = os.getenv("PLAN_MODEL_API_KEY", "").strip()
    if base_url and api_key:
        return ModelEndpointConfig(base_url, api_key, "", "plan_gateway")

    legacy_key = os.getenv("GPT_APIKEY", "").strip()
    if legacy_key:
        return ModelEndpointConfig(DEFAULT_TESTAI_BASE_URL, legacy_key, "", "testai_legacy")
    return None


def load_selected_plan_model(selection_file: Path | None = None) -> str:
    selection_file = selection_file or runtime_data_dir() / "model-selection.json"
    try:
        payload = json.loads(selection_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(payload.get("model") or "").strip()


def save_selected_plan_model(model: str, selection_file: Path | None = None) -> None:
    selection_file = selection_file or runtime_data_dir() / "model-selection.json"
    selection_file.parent.mkdir(parents=True, exist_ok=True)
    selection_file.write_text(
        json.dumps({"model": model}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_vision_model_config() -> ModelEndpointConfig | None:
    """Load the multimodal model used only for runtime visual fallback."""
    saved = load_saved_settings("vision")
    if saved and all((saved["base_url"], saved["api_key"], saved["model"])):
        return ModelEndpointConfig(
            saved["base_url"], saved["api_key"], saved["model"], "workbench"
        )
    return _load_endpoint(
        (
            ("vision_model", "VISION_MODEL_BASE_URL", "VISION_MODEL_API_KEY", "VISION_MODEL_NAME"),
            ("shared_ai", "AI_BASE_URL", "AI_API_KEY", "AI_VISION_MODEL"),
            ("legacy_shared", "CHATMODEL_VL_BASE_URL", "CHATMODEL_VL_APIKEY", "CHATMODEL_VL_MODEL"),
        )
    )


def load_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return min(max(value, minimum), maximum)


def model_config_status() -> dict[str, object]:
    plan_gateway = load_plan_gateway_config()
    plan = load_plan_model_config()
    vision = load_vision_model_config()
    return {
        "plan_model": {
            **(plan.public_status() if plan else {"configured": False}),
            "gateway_configured": bool(plan_gateway),
            "gateway_source": plan_gateway.source if plan_gateway else None,
        },
        "vision_model": {
            **(vision.public_status() if vision else {"configured": False}),
            "minimum_confidence": load_float(
                "VISION_MODEL_MIN_CONFIDENCE", 0.85, minimum=0.0, maximum=1.0
            ),
            "timeout_seconds": load_float(
                "VISION_MODEL_TIMEOUT_SECONDS", 90.0, minimum=1.0, maximum=300.0
            ),
        },
    }
