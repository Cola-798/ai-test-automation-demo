from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .schemas import DevicePlatform, PlanRecord
from .runtime import AppiumRuntimeSession, run_plan


class DeviceBusyError(RuntimeError):
    pass


def acquire_device_lease() -> Callable[[], None]:
    # Appium is shared locally; serialize all Demo operations before opening it.
    path = Path(tempfile.gettempdir()) / "app-automation-poc-device.lock"
    handle = path.open("a+b")
    if path.stat().st_size == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise DeviceBusyError("测试设备正被其他执行任务占用（含其他 Demo 服务）") from exc
    return handle.close


class DeviceExecutionCoordinator:
    """Prevent two requests from driving the single configured device at once."""

    def __init__(self, execute: Callable[..., dict]):
        self._execute = execute
        self._lock = threading.Lock()

    def run(self, record: PlanRecord, platform: DevicePlatform | str = DevicePlatform.ANDROID,
            device_udid: str | None = None) -> dict:
        release = self.reserve()
        try:
            selected_platform = DevicePlatform(platform)
            if device_udid is None:
                return self._execute(record, selected_platform)
            return self._execute(record, selected_platform, device_udid)
        finally:
            release()

    def reserve(self) -> Callable[[], None]:
        if not self._lock.acquire(blocking=False):
            raise DeviceBusyError("测试设备正被其他执行任务占用")
        try:
            release_lease = acquire_device_lease()
        except Exception:
            self._lock.release()
            raise
        released = False

        def release() -> None:
            nonlocal released
            if not released:
                released = True
                release_lease()
                self._lock.release()
        return release

    @contextmanager
    def batch(self, runner_factory: Callable[[], Any]):
        release = self.reserve()
        try:
            with runner_factory() as runner:
                yield runner
        finally:
            release()

    @contextmanager
    def reserved_batch(self, runner_factory: Callable[[], Any]):
        """Create the batch session while the caller holds a reservation."""
        with runner_factory() as runner:
            yield runner


@contextmanager
def reusable_plan_runner(
    runs_dir: Path,
    *,
    platform: DevicePlatform | str = DevicePlatform.ANDROID,
    device_udid: str | None = None,
    on_result_saved: Callable[[str], None] | None = None,
):
    """Execute a serial batch in-process while retaining one Appium session."""
    session = None
    try:
        def execute(record: PlanRecord) -> dict:
            nonlocal session
            session = session or AppiumRuntimeSession(platform, device_udid=device_udid)
            run_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}"
            run_dir = runs_dir / run_id
            run_dir.mkdir(parents=True, exist_ok=False)
            plan_path = run_dir / "plan.json"
            plan_path.write_text(record.plan.model_dump_json(indent=2), encoding="utf-8")
            result = run_plan(record.plan, run_dir, runtime_session=session)
            result["run_id"] = run_id
            result["report_url"] = f"/runs/{run_id}/report.html"
            result_path = run_dir / "result.json"
            result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            if on_result_saved:
                on_result_saved(run_id)
            return result

        yield execute
    finally:
        if session is not None:
            session.close()


def execute_plan_record(
    record: PlanRecord,
    runs_dir: Path,
    *,
    project_root: Path,
    platform: DevicePlatform | str = DevicePlatform.ANDROID,
    device_udid: str | None = None,
    timeout_seconds: int = 300,
    evidence_only: bool = False,
) -> dict:
    """Run one confirmed plan through the existing pytest/Appium runtime."""
    run_id = f"{'evidence-' if evidence_only else ''}{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}"
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    plan_path = run_dir / "plan.json"
    plan_path.write_text(record.plan.model_dump_json(indent=2), encoding="utf-8")
    command = [
        sys.executable,
        "-m",
        "pytest",
        "tests/test_execution.py",
        f"--demo-plan-file={plan_path}",
        f"--demo-output-dir={run_dir}",
        "-q",
    ]
    confcutdir = project_root / "tests" / "runtime"
    if confcutdir.is_dir():
        command.insert(4, f"--confcutdir={confcutdir}")
    execution_environment = os.environ.copy()
    execution_environment["MOBILE_PLATFORM"] = DevicePlatform(platform).value
    if device_udid:
        execution_environment["ANDROID_DEVICE_UDID"] = device_udid
    completed = subprocess.run(
        command,
        cwd=project_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        check=False,
        env=execution_environment,
    )
    result_path = run_dir / "result.json"
    if not result_path.exists():
        raise RuntimeError(f"pytest 未生成结构化结果：{completed.stderr[-1000:]}")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["run_id"] = run_id
    if evidence_only:
        result["evidence_only"] = True
    result["report_url"] = f"/runs/{run_id}/report.html"
    result["pytest"] = {
        "exit_code": completed.returncode,
        "summary": (completed.stdout + completed.stderr)[-3000:],
    }
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
