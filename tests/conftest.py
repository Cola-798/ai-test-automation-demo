from __future__ import annotations

from pathlib import Path

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--demo-plan-file", action="store")
    parser.addoption("--demo-output-dir", action="store")


@pytest.fixture(scope="session")
def demo_plan_file(pytestconfig: pytest.Config) -> Path:
    value = pytestconfig.getoption("--demo-plan-file")
    if not value:
        pytest.skip("正式真机执行入口仅在传入 --demo-plan-file 时运行")
    return Path(value)


@pytest.fixture(scope="session")
def demo_output_dir(pytestconfig: pytest.Config) -> Path:
    value = pytestconfig.getoption("--demo-output-dir")
    if not value:
        pytest.skip("正式真机执行入口仅在传入 --demo-output-dir 时运行")
    return Path(value)
