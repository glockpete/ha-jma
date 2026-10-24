"""Fixture loading for the parser tests (samples of the agency's published files)."""

from __future__ import annotations

import copy
import json
import re
from datetime import date, datetime
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


def wbgt_on(name: str, first_day: date) -> str:
    """A captured WBGT forecast file with its dates moved so that its first day is `first_day` (the captures are
    from the end of the 2026 season; the tests run inside it). Only the dates move: the point rows, the hours and the
    values are the capture's own, and so is the row order."""
    text = load_text(name)
    header, _, rows = text.partition("\n")
    first = datetime.strptime(header.split(",")[2][:8], "%Y%m%d").date()
    shift = first_day - first

    def moved_label(match: re.Match[str]) -> str:
        return (datetime.strptime(match.group(1), "%Y%m%d").date() + shift).strftime("%Y%m%d") + match.group(2)

    def moved_issue(match: re.Match[str]) -> str:
        return (datetime.strptime(match.group(1), "%Y/%m/%d").date() + shift).strftime("%Y/%m/%d")

    return re.sub(r"(?<=,)(\d{8})(\d{2})(?=,|$)", moved_label, header) + "\n" + re.sub(
        r"(\d{4}/\d{2}/\d{2})(?= \d{2}:\d{2})", moved_issue, rows)
