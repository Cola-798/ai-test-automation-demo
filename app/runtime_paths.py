import os
from pathlib import Path


def runtime_data_dir() -> Path:
    return Path(os.environ.get("DEMO_RUNTIME_DIR", str(Path(__file__).resolve().parent.parent / "runtime-data")))
