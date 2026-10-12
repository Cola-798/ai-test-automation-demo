from __future__ import annotations

import json
import os
from dataclasses import dataclass

from .runtime_paths import runtime_data_dir
from .schemas import DevicePlatform, Locator, LocatorStrategy


@dataclass(frozen=True)
class LocatorTarget:
    key: str
    description: str
    android_locators: tuple[Locator, ...]
    starts_activity: bool = False
    refresh_after_tap: bool = False
    ios_locators: tuple[Locator, ...] = ()

    def for_platform(self, platform: DevicePlatform) -> tuple[Locator, ...]:
        return self.ios_locators if platform == DevicePlatform.IOS else self.android_locators


def _id(value: str) -> Locator:
    return Locator(strategy=LocatorStrategy.RESOURCE_ID, value=f"com.example.app1:id/{value}")


def _text(value: str) -> Locator:
    return Locator(strategy=LocatorStrategy.TEXT, value=value)


def _resource_id(value: str) -> Locator:
    return Locator(strategy=LocatorStrategy.RESOURCE_ID, value=value)


def _accessibility_id(value: str) -> Locator:
    return Locator(strategy=LocatorStrategy.ACCESSIBILITY_ID, value=value)


def _uiautomator(value: str) -> Locator:
    return Locator(strategy=LocatorStrategy.ANDROID_UIAUTOMATOR, value=value)


def _xpath(value: str) -> Locator:
    return Locator(strategy=LocatorStrategy.XPATH, value=value)


# App-specific locator data is supplied by the user at runtime.


def target_exists(target_key: str) -> bool:
    return target_key in active_targets()


def _app_catalog_payload() -> dict | None:
    package = os.getenv("ANDROID_APP_PACKAGE", "com.example.app1")
    path = runtime_data_dir() / "locator_catalog.json"
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("app_package") != package:
        raise ValueError(f"定位表应用包名与当前设备配置不一致：{path}")
    return payload


def active_targets() -> dict[str, LocatorTarget]:
    payload = _app_catalog_payload()
    targets = {
        key: LocatorTarget(
            key=key,
            description=item["description"],
            android_locators=tuple(Locator.model_validate(value) for value in item.get("android_locators", [])),
            ios_locators=tuple(Locator.model_validate(value) for value in item.get("ios_locators", [])),
            starts_activity=bool(item.get("starts_activity", False)),
            refresh_after_tap=bool(item.get("refresh_after_tap", False)),
        )
        for key, item in payload.get("targets", {}).items()
    }
    from .collection import published_collection
    try:
        collected = published_collection(runtime_data_dir(), os.getenv("ANDROID_APP_PACKAGE", "com.example.app1"))
    except ValueError as exc:
        import warnings
        warnings.warn(str(exc), RuntimeWarning, stacklevel=2)
        return targets
    for key, item in collected["targets"].items():
        receipt = item.get("verification", {})
        if key not in targets and receipt.get("status") == "locator_verified" and receipt.get("matched_count") == 1:
            targets[key] = LocatorTarget(key, item["description"],
                                        tuple(Locator.model_validate(v) for v in item["android_locators"]))
    return targets


def app_navigation_routes() -> list[str] | None:
    payload = _app_catalog_payload()
    return list(payload.get("navigation_routes", []))


def saved_widget_locator(target_key: str | None, match: dict[str, str]) -> Locator:
    """Scope known saved-widget fields to a row backed by captured Android XML."""
    fields = {"plan_name": "example_element_132", "widget_name": "example_element_149"}
    targets = {"saved_widget.preview": "example_element_067", "saved_widget.name": "example_element_149",
               "saved_widget.plan_name": "example_element_132", "saved_widget.delete": "example_element_025"}
    if target_key not in targets or not isinstance(match, dict) or not match or set(match) - fields.keys():
        raise ValueError("组件筛选仅支持已验证的组件目标与 plan_name/widget_name")
    def literal(value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("组件筛选内容不能为空")
        if "'" not in value:
            return "'" + value + "'"
        if '"' not in value:
            return '"' + value + '"'
        return "concat(" + ',"\'",'.join("'" + part + "'" for part in value.split("'")) + ")"
    predicates = [f".//*[@resource-id='com.example.app1:id/{fields[key]}' and @text={literal(value)}]"
                  for key, value in sorted(match.items())]
    return _xpath("//*[@resource-id='com.example.app1:id/example_element_132']/parent::*["
                  + " and ".join(predicates) + "]//*[@resource-id='com.example.app1:id/"
                  + targets[target_key] + "']")


def resolve_target(
    target_key: str, platform: DevicePlatform | str = DevicePlatform.ANDROID
) -> tuple[Locator, ...]:
    try:
        target = active_targets()[target_key]
    except KeyError as exc:
        raise KeyError(f"定位库中不存在目标：{target_key}") from exc
    selected = DevicePlatform(platform)
    locators = target.for_platform(selected)
    if not locators:
        raise LookupError(f"目标 {target_key} 尚未配置 {selected.value} 定位策略")
    return locators


def target_starts_activity(
    target_key: str | None, platform: DevicePlatform | str = DevicePlatform.ANDROID
) -> bool:
    targets = active_targets()
    return bool(
        DevicePlatform(platform) == DevicePlatform.ANDROID
        and target_key
        and target_key in targets
        and targets[target_key].starts_activity
    )


def target_refresh_reason(
    target_key: str | None, platform: DevicePlatform | str = DevicePlatform.ANDROID
) -> str | None:
    targets = active_targets()
    if DevicePlatform(platform) != DevicePlatform.ANDROID or not target_key or target_key not in targets:
        return None
    target = targets[target_key]
    if target.starts_activity:
        return "activity_transition"
    if target.refresh_after_tap:
        return "context_transition"
    return None


def post_tap_page_marker(
    target_key: str | None, platform: DevicePlatform | str = DevicePlatform.ANDROID
) -> str | None:
    if DevicePlatform(platform) != DevicePlatform.ANDROID:
        return None
    payload = _app_catalog_payload()
    markers = payload.get("post_tap_page_markers", {})
    return markers.get(target_key or "")


def compiler_catalog() -> list[dict]:
    return [
        {
            "target_key": item.key,
            "description": item.description,
            "android_locators": [locator.model_dump(mode="json") for locator in item.android_locators],
            "configured_platforms": ",".join(
                platform.value
                for platform in DevicePlatform
                if item.for_platform(platform)
            ),
        }
        for item in active_targets().values()
    ]


def locator_coverage() -> dict[str, dict[str, int]]:
    targets = active_targets()
    return {
        platform.value: {
            "configured_targets": sum(bool(item.for_platform(platform)) for item in targets.values()),
            "total_targets": len(targets),
        }
        for platform in DevicePlatform
    }
