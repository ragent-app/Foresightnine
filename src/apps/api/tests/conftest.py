from __future__ import annotations

from pathlib import Path
import sys

import pytest

API_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = API_ROOT.parents[1]
sys.path.insert(0, str(API_ROOT))

from app.main import reset_state


@pytest.fixture(autouse=True)
def clean_in_memory_state() -> None:
    reset_state()
