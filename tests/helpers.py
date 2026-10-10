"""Fixture loading for the parser tests (samples of the agency's published files)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures"


def load_json(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def load_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def load_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def clone(obj: Any) -> Any:
    return copy.deepcopy(obj)
