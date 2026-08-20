import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import load_config  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def cfg():
    return load_config(PROJECT_ROOT / "config.yaml")
