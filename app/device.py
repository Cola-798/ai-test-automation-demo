from __future__ import annotations

import os
import re
import socket
import subprocess
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias

from appium import webdriver
from appium.webdriver.client_config import AppiumClientConfig
from appium.options.android import UiAutomator2Options
from appium.options.ios import XCUITestOptions

from .schemas import DevicePlatform


@dataclass(frozen=True)
class AndroidDeviceConfig:
    server_url: str = "http://127.0.0.1:4723"
    udid: str = ""
    device_name: str = ""
    package: str = "com.example.app1"
    activity: str = ".MainActivity"
    server_read_timeout_ms: int = 60000
    server_launch_timeout_ms: int = 90000
    command_timeout_seconds: int = 30
    platform: DevicePlatform = DevicePlatform.ANDROID

    @property
    def app_identifier(self) -> str:
        return self.package

    @property
    def configured(self) -> bool:
        return all((self.server_url, self.udid, self.device_name, self.package, self.activity))

    @classmethod
    def from_environment(cls) -> "AndroidDeviceConfig":
        return cls(
            server_url=os.getenv("ANDROID_APPIUM_SERVER_URL", os.getenv("APPIUM_SERVER_URL", cls.server_url)),
            udid=os.getenv("ANDROID_DEVICE_UDID", cls.udid),
            device_name=os.getenv("ANDROID_DEVICE_NAME", cls.device_name),
            package=os.getenv("ANDROID_APP_PACKAGE", cls.package),
            activity=os.getenv("ANDROID_APP_ACTIVITY", cls.activity),
            server_read_timeout_ms=int(
                os.getenv("ANDROID_UIAUTOMATOR2_READ_TIMEOUT_MS", str(cls.server_read_timeout_ms))
            ),
            server_launch_timeout_ms=int(
                os.getenv("ANDROID_UIAUTOMATOR2_LAUNCH_TIMEOUT_MS", str(cls.server_launch_timeout_ms))
            ),
            command_timeout_seconds=int(
                os.getenv("ANDROID_APPIUM_COMMAND_TIMEOUT_SECONDS", str(cls.command_timeout_seconds))
            ),
        )


@dataclass(frozen=True)
class IOSDeviceConfig:
    """Configuration for a remote Appium/XCUITest service running on macOS."""

    server_url: str = ""
    udid: str = ""
    device_name: str = ""
    bundle_id: str = ""
    platform_version: str = ""
    xcode_org_id: str = ""
    xcode_signing_id: str = "Apple Development"
    updated_wda_bundle_id: str = ""
    platform: DevicePlatform = DevicePlatform.IOS

    @property
    def app_identifier(self) -> str:
        return self.bundle_id

    @property
    def configured(self) -> bool:
        return all((self.server_url, self.udid, self.device_name, self.bundle_id))

    @classmethod
    def from_environment(cls) -> "IOSDeviceConfig":
        return cls(
            server_url=os.getenv("IOS_APPIUM_SERVER_URL", "").strip(),
            udid=os.getenv("IOS_DEVICE_UDID", "").strip(),
            device_name=os.getenv("IOS_DEVICE_NAME", "").strip(),
            bundle_id=os.getenv("IOS_APP_BUNDLE_ID", "").strip(),
            platform_version=os.getenv("IOS_PLATFORM_VERSION", "").strip(),
            xcode_org_id=os.getenv("IOS_XCODE_ORG_ID", "").strip(),
            xcode_signing_id=os.getenv("IOS_XCODE_SIGNING_ID", cls.xcode_signing_id).strip(),
            updated_wda_bundle_id=os.getenv("IOS_UPDATED_WDA_BUNDLE_ID", "").strip(),
        )


MobileDeviceConfig: TypeAlias = AndroidDeviceConfig | IOSDeviceConfig


def android_system_port(udid: str) -> int:
    """Give each Android device a stable UiAutomator2 forwarding port."""
    return 8201 + zlib.crc32(udid.encode("utf-8")) % 99


def available_android_system_port(udid: str) -> int:
    preferred = android_system_port(udid)
    for offset in range(99):
        port = 8201 + ((preferred - 8201 + offset) % 99)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise RuntimeError("没有可用的 UiAutomator2 systemPort（8201-8299）")


def _adb_path() -> Path:
    configured = os.getenv("ADB_PATH", "").strip()
    if configured:
        return Path(configured)
    return Path("adb")


def connected_android_devices(adb_path: Path | None = None) -> list[dict[str, Any]]:
    """Discover Android devices from the local ADB server.

    Unauthorized and offline devices are returned for visibility, but only
    devices with status ``device`` can be selected for execution.
    """
    adb = adb_path or _adb_path()
    try:
        completed = subprocess.run(
            [str(adb), "devices", "-l"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    devices: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("list of devices"):
            continue
        match = re.match(r"^(\S+)\s+(\S+)(?:\s+(.*))?$", line)
        if not match:
            continue
        udid, status, details = match.groups()
        detail_map: dict[str, str] = {}
        for token in (details or "").split():
            if ":" in token:
                key, value = token.split(":", 1)
                detail_map[key] = value
        item: dict[str, Any] = {
            "udid": udid,
            "status": status,
            "authorized": status == "device",
            "brand": detail_map.get("brand", ""),
            "model": detail_map.get("model", detail_map.get("product", "")),
            "product": detail_map.get("product", ""),
            "device": detail_map.get("device", ""),
        }
        if status == "device":
            try:
                props = subprocess.run(
                    [str(adb), "-s", udid, "shell", "getprop"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=8,
                    check=False,
                ).stdout
                for prop, value in re.findall(r"\[([^]]+)\]: \[([^]]*)\]", props):
                    if prop == "ro.product.brand":
                        item["brand"] = value
                    elif prop == "ro.product.model":
                        item["model"] = value
                    elif prop == "ro.build.version.release":
                        item["android_version"] = value
                    elif prop == "ro.build.version.sdk":
                        item["sdk"] = value
            except (OSError, subprocess.TimeoutExpired):
                pass
        item["label"] = " ".join(part for part in (item.get("brand"), item.get("model")) if part) or udid
        devices.append(item)
    return devices


def configured_platform() -> DevicePlatform:
    raw = os.getenv("MOBILE_PLATFORM", DevicePlatform.ANDROID.value).strip().lower()
    try:
        return DevicePlatform(raw)
    except ValueError as exc:
        raise RuntimeError(f"不支持的移动设备平台：{raw}") from exc


def device_config_for_platform(
    platform: DevicePlatform | str,
    device_udid: str | None = None,
) -> MobileDeviceConfig:
    selected = DevicePlatform(platform)
    if selected == DevicePlatform.IOS:
        config = IOSDeviceConfig.from_environment()
        if not config.configured:
            raise RuntimeError(
                "iOS 执行环境尚未配置：需要 IOS_APPIUM_SERVER_URL、IOS_DEVICE_UDID、"
                "IOS_DEVICE_NAME 和 IOS_APP_BUNDLE_ID"
            )
        return config
    configured = AndroidDeviceConfig.from_environment()
    devices = [item for item in connected_android_devices() if item["status"] == "device"]
    selected_udid = (device_udid or os.getenv("ANDROID_DEVICE_UDID", "").strip()).strip()
    if selected_udid:
        selected = next((item for item in devices if item["udid"] == selected_udid), None)
        if selected is None:
            visible = next((item for item in connected_android_devices() if item["udid"] == selected_udid), None)
            if visible and visible["status"] == "unauthorized":
                raise RuntimeError(f"Android 设备 {selected_udid} 尚未授权 USB 调试，请在手机上确认授权")
            raise RuntimeError(f"未找到可执行的 Android 设备：{selected_udid}")
    elif len(devices) == 1:
        selected = devices[0]
    elif len(devices) > 1:
        raise RuntimeError("检测到多台 Android 设备，请先选择执行设备")
    else:
        raise RuntimeError("未检测到已授权的 Android 设备，请连接设备并确认 USB 调试授权")
    config = configured.__class__(
        server_url=configured.server_url,
        udid=selected["udid"],
        device_name=selected.get("label") or selected["udid"],
        package=configured.package,
        activity=configured.activity,
        server_read_timeout_ms=configured.server_read_timeout_ms,
        server_launch_timeout_ms=configured.server_launch_timeout_ms,
        command_timeout_seconds=configured.command_timeout_seconds,
    )
    return config


def platform_status(*, include_ios: bool = True) -> list[dict[str, Any]]:
    android = AndroidDeviceConfig.from_environment()
    ios = IOSDeviceConfig.from_environment()
    devices = connected_android_devices()
    authorized = next((item for item in devices if item["status"] == "device"), None)
    android_ready = bool(authorized and android.server_url and android.package and android.activity)
    android_item = {
            "platform": android.platform.value,
            "label": "Android",
            "configured": android_ready,
            "verified": bool(authorized),
            "device_name": authorized.get("label", "") if authorized else android.device_name,
            "selected_device_udid": authorized.get("udid") if authorized else None,
            "server_url": android.server_url,
            "message": "Android 设备已连接；Appium 将在执行前检查" if android_ready else "等待已授权 Android 设备",
            "devices": devices,
        }
    result = [android_item]
    if include_ios:
        result.append({
            "platform": ios.platform.value,
            "label": "iOS",
            "configured": ios.configured,
            "verified": False,
            "device_name": ios.device_name,
            "server_url": ios.server_url,
            "message": "已配置，等待真机验证" if ios.configured else "等待配置远程 Mac Appium 与 iPhone",
        })
    return result


class AppiumDeviceSession:
    def __init__(self, config: MobileDeviceConfig | None = None):
        self.config = config or device_config_for_platform(configured_platform())

    def open(self, *, launch_app: bool) -> Any:
        if isinstance(self.config, IOSDeviceConfig):
            return self._open_ios(launch_app=launch_app)
        return self._open_android(launch_app=launch_app)

    def _open_android(self, *, launch_app: bool) -> Any:
        config = self.config
        assert isinstance(config, AndroidDeviceConfig)
        capabilities: dict[str, Any] = {
            "platformName": "Android",
            "appium:automationName": "UiAutomator2",
            "appium:udid": config.udid,
            "appium:deviceName": config.device_name,
            "appium:systemPort": available_android_system_port(config.udid),
            "appium:noReset": True,
            # The Android device blocks Appium's optional settings APK install;
            # the UiAutomator2 server is already present on the device.
            "appium:skipDeviceInitialization": True,
            "appium:newCommandTimeout": 120,
            "appium:uiautomator2ServerReadTimeout": config.server_read_timeout_ms,
            "appium:uiautomator2ServerLaunchTimeout": config.server_launch_timeout_ms,
            "appium:disableWindowAnimation": True,
            "appium:settings[waitForIdleTimeout]": 0,
            "appium:settings[waitForSelectorTimeout]": 1000,
        }
        if launch_app:
            capabilities.update(
                {
                    "appium:appPackage": config.package,
                    "appium:appActivity": config.activity,
                    "appium:appWaitActivity": "*",
                    "appium:forceAppLaunch": True,
                }
            )
        options = UiAutomator2Options().load_capabilities(capabilities)
        client_config = AppiumClientConfig(config.server_url, timeout=config.command_timeout_seconds)
        return webdriver.Remote(config.server_url, options=options, client_config=client_config)

    def _open_ios(self, *, launch_app: bool) -> Any:
        config = self.config
        assert isinstance(config, IOSDeviceConfig)
        capabilities: dict[str, Any] = {
            "platformName": "iOS",
            "appium:automationName": "XCUITest",
            "appium:udid": config.udid,
            "appium:deviceName": config.device_name,
            "appium:noReset": True,
            "appium:newCommandTimeout": 120,
            "appium:useNewWDA": False,
            "appium:wdaLaunchTimeout": 120000,
            "appium:wdaConnectionTimeout": 120000,
        }
        if config.platform_version:
            capabilities["appium:platformVersion"] = config.platform_version
        if config.xcode_org_id:
            capabilities["appium:xcodeOrgId"] = config.xcode_org_id
            capabilities["appium:xcodeSigningId"] = config.xcode_signing_id
        if config.updated_wda_bundle_id:
            capabilities["appium:updatedWDABundleId"] = config.updated_wda_bundle_id
        if launch_app:
            capabilities["appium:bundleId"] = config.bundle_id
        options = XCUITestOptions().load_capabilities(capabilities)
        return webdriver.Remote(config.server_url, options=options)

    def refresh_after_activity(self, driver: Any, settle_seconds: float) -> Any:
        # Android uses this after verified Activity/context transitions. The iOS
        # executor does not request this refresh because XCUITest has no Activity concept.
        try:
            driver.quit()
        except Exception:
            pass
        time.sleep(settle_seconds)
        return self.open(launch_app=False)
