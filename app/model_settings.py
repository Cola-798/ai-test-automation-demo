from __future__ import annotations

import base64
import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

from .runtime_paths import runtime_data_dir


class ModelSettingsInput(BaseModel):
    base_url: str = Field(min_length=8, max_length=500)
    api_key: str = Field(default="", max_length=2000)
    model: str = Field(default="", max_length=200)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("模型地址必须是完整的 HTTP 或 HTTPS URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("模型地址不能包含账号、密码、查询参数或片段")
        return normalized


class ModelConnectionInput(ModelSettingsInput):
    pass


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def protect_secret(secret: str) -> str:
    if os.name != "nt":
        raise RuntimeError("当前密钥保护实现仅支持 Windows DPAPI")
    source, source_buffer = _blob(secret.encode("utf-8"))
    target = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    if not crypt32.CryptProtectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)
    ):
        raise ctypes.WinError()
    try:
        encrypted = ctypes.string_at(target.pbData, target.cbData)
        return base64.b64encode(encrypted).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)
        _ = source_buffer


def unprotect_secret(protected: str) -> str:
    if os.name != "nt":
        raise RuntimeError("当前密钥保护实现仅支持 Windows DPAPI")
    source, source_buffer = _blob(base64.b64decode(protected))
    target = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.pbData, target.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)
        _ = source_buffer


def _read_file(settings_file: Path) -> dict:
    try:
        payload = json.loads(settings_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def load_saved_settings(
    role: Literal["plan", "vision"], settings_file: Path | None = None
) -> dict[str, str] | None:
    settings_file = settings_file or runtime_data_dir() / "model-settings.json"
    item = _read_file(settings_file).get(role)
    if not isinstance(item, dict):
        return None
    protected = str(item.get("api_key_protected") or "")
    try:
        api_key = unprotect_secret(protected) if protected else ""
    except Exception:
        api_key = ""
    return {
        "base_url": str(item.get("base_url") or "").strip().rstrip("/"),
        "api_key": api_key,
        "model": str(item.get("model") or "").strip(),
    }


def save_model_settings(
    role: Literal["plan", "vision"],
    settings: ModelSettingsInput,
    settings_file: Path | None = None,
) -> dict[str, str]:
    settings_file = settings_file or runtime_data_dir() / "model-settings.json"
    payload = _read_file(settings_file)
    current = payload.get(role) if isinstance(payload.get(role), dict) else {}
    protected = str(current.get("api_key_protected") or "")
    if settings.api_key:
        protected = protect_secret(settings.api_key)
    if not protected:
        raise ValueError("首次保存必须填写 API Key")
    payload[role] = {
        "base_url": settings.base_url,
        "model": settings.model.strip(),
        "api_key_protected": protected,
    }
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = settings_file.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(settings_file)
    return {"base_url": settings.base_url, "model": settings.model.strip()}


def public_saved_settings(
    role: Literal["plan", "vision"], settings_file: Path | None = None
) -> dict[str, str | bool] | None:
    settings = load_saved_settings(role, settings_file)
    if settings is None:
        return None
    return {
        "base_url": settings["base_url"],
        "model": settings["model"],
        "api_key_configured": bool(settings["api_key"]),
        "source": "workbench",
    }
