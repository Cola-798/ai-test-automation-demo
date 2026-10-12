"""Check the configured Appium server and recover a stopped local service."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx


_start_lock = threading.Lock()
_LOCAL_HOSTS = {"127.0.0.1", "localhost"}


def appium_ready(server_url: str) -> bool:
    try:
        response = httpx.get(server_url.rstrip("/") + "/status", timeout=2)
        response.raise_for_status()
        return response.json().get("value", {}).get("ready") is True
    except (httpx.HTTPError, ValueError, AttributeError):
        return False


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _start_local_appium(port: int, base_path: str, log_dir: Path) -> None:
    command = shutil.which("appium.cmd") or shutil.which("appium")
    if not command:
        raise RuntimeError("Appium 未运行，且本机找不到 appium 命令；请安装 Appium Server")
    log_dir.mkdir(parents=True, exist_ok=True)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    with (log_dir / "appium-recovery.stdout.log").open("a", encoding="utf-8") as stdout, \
         (log_dir / "appium-recovery.stderr.log").open("a", encoding="utf-8") as stderr:
        subprocess.Popen(
            [command, "--address", "127.0.0.1", "--port", str(port), "--base-path", base_path,
             "--session-override", "--log-level", "info", "--log-no-colors"],
            cwd=Path(__file__).resolve().parent.parent,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            creationflags=flags,
            start_new_session=os.name != "nt",
        )


def ensure_appium_ready(server_url: str, log_dir: Path, *, timeout_seconds: float = 15) -> None:
    """Only start a local service when its port is closed; never take over another service."""
    parsed = urlparse(server_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError(f"Appium 服务地址无效：{server_url}")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise RuntimeError(f"Appium 服务端口无效：{server_url}") from exc
    if appium_ready(server_url):
        return
    with _start_lock:
        if appium_ready(server_url):
            return
        if parsed.hostname not in _LOCAL_HOSTS:
            raise RuntimeError(f"远程 Appium 未就绪：{server_url}；请检查远程服务")
        if _port_open(parsed.hostname, port):
            raise RuntimeError(f"{server_url} 端口已被占用，但 Appium 未就绪；已停止执行，避免误启动第二个服务")
        try:
            _start_local_appium(port, parsed.path.rstrip("/") or "/", log_dir)
        except OSError as exc:
            raise RuntimeError(f"本机 Appium 自动启动失败：{exc}") from exc
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if appium_ready(server_url):
                return
            time.sleep(0.5)
        raise RuntimeError(f"本机 Appium 启动后仍未就绪：{server_url}；请查看 {log_dir} 中的启动日志")
