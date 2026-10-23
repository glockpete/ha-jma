"""Parse JMA's warning timeline (危険度の時系列): the 3-hourly warning-risk outlook. No HA imports.

File: /bosai/warning_timeline/data/{office}.json, a dict with `reportDatetime` and `timeSeries[]`.
Each series has `timeDefines` [{dateTime, duration}] and `class20Items` [{areaCode, kinds}]; a kind
carries `significancyParts` (a risk code per step) and/or `forecastParts` (values per step).
The file holds one PT3H series (8 steps, 24 h) and two PT24H series (daily). Series are told
apart by their duration, never by position.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .const import (
    TIMELINE_CODE_NOT_APPLICABLE,
    TIMELINE_LEVELS,
    TIMELINE_PHENOMENA,
    TIMELINE_SEA_LOCAL,
    TIMELINE_TEXT_VALUES,
    TIMELINE_VALUES,
)
from .errors import JmaParseError

_LOGGER = logging.getLogger(__name__)
_logged_unknown: set[str] = set()
_DURATION = re.compile(r"^PT(\d+)H$")


def significancy_level(code: Any) -> int | None:
    """Level for one significancy code; None for "00" (not applicable to this area/step)."""
    if code == TIMELINE_CODE_NOT_APPLICABLE:
        return None
    return TIMELINE_LEVELS.get(code, 0) if isinstance(code, str) else 0


def _phenomenon(kind_type: str) -> str:
    key = TIMELINE_PHENOMENA.get(kind_type)
    if key is None:
        if kind_type not in _logged_unknown:
            _logged_unknown.add(kind_type)
            _LOGGER.warning("Unknown warning-timeline type %r kept under its raw name", kind_type)
        return kind_type
    return key


def _hours(duration: Any) -> int:
    m = _DURATION.match(duration) if isinstance(duration, str) else None
    if not m:
        raise JmaParseError(f"warning timeline: duration {duration!r}")
    return int(m.group(1))


def _land_locals(locals_: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The configured place is on land: drop 海上 locals unless nothing else is there."""
    land = [loc for loc in locals_ if loc.get("areaName") != TIMELINE_SEA_LOCAL]
    return land or locals_


def _number(value: Any) -> int | float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        f = float(value)
    except ValueError:
        return None
    return int(f) if f.is_integer() else f


@dataclass(frozen=True)
class WarningOutlook:
    """The class20 area's outlook: 3-hourly steps + daily rows."""

    report_time: str | None
    level: int
    steps: list[dict[str, Any]]
    daily: list[dict[str, Any]]
    peak: dict[str, Any] | None


def _area_kinds(series: dict[str, Any], area: str) -> list[dict[str, Any]] | None:
    for item in series.get("class20Items") or []:
        if item.get("areaCode") == area:
            return item.get("kinds") or []
    return None


def _levels_per_step(kinds: list[dict[str, Any]], n: int) -> list[dict[str, int]]:
    out: list[dict[str, int]] = [{} for _ in range(n)]
    for kind in kinds:
        for part in kind.get("significancyParts") or []:
            phen = _phenomenon(part["type"])
            for loc in _land_locals(part["locals"]):
                for i, code in enumerate(loc["codes"][:n]):
                    level = significancy_level(code)
                    if level is not None and level >= 2:
                        out[i][phen] = max(out[i].get(phen, 0), level)
    return out


def _values_per_step(kinds: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{field: None for field in TIMELINE_VALUES.values()} for _ in range(n)]
    for kind in kinds:
        for part in kind.get("forecastParts") or []:
            field_name = TIMELINE_VALUES.get(part["type"])
            if field_name is None:
                continue
            loc = _land_locals(part["locals"])[0]
            for i, v in enumerate(loc["values"][:n]):
                raw = v.get("value") if isinstance(v, dict) else None
                if field_name in TIMELINE_TEXT_VALUES:
                    out[i][field_name] = raw or None
                else:
                    out[i][field_name] = _number(raw)
    return out


def parse_timeline(data: Any, area: str) -> WarningOutlook:
    """Outlook for one class20 area. State = highest level (0/2..5) over the 3-hourly steps."""
    try:
        steps_by_start: dict[str, dict[str, Any]] = {}
        daily_by_date: dict[str, dict[str, int]] = {}
        area_seen = False
        for series in data["timeSeries"]:
            defines = series["timeDefines"]
            kinds = _area_kinds(series, area)
            if kinds is None:
                continue
            area_seen = True
            n = len(defines)
            hours = {_hours(d["duration"]) for d in defines}
            levels = _levels_per_step(kinds, n)
            if hours == {3}:
                values = _values_per_step(kinds, n)
                for i, d in enumerate(defines):
                    start = datetime.fromisoformat(d["dateTime"])
                    step = steps_by_start.setdefault(start.isoformat(), {
                        "start": start.isoformat(),
                        "end": (start + timedelta(hours=3)).isoformat(),
                        "levels": {},
                        **{f: None for f in TIMELINE_VALUES.values()},
                    })
                    for phen, lv in levels[i].items():
                        step["levels"][phen] = max(step["levels"].get(phen, 0), lv)
                    for f, v in values[i].items():
                        if v is not None:
                            step[f] = v
            elif hours == {24}:
                has_levels = any(k.get("significancyParts") for k in kinds)
                if not has_levels:
                    continue  # 24-h rain/snow totals only; they carry no level
                for i, d in enumerate(defines):
                    day = datetime.fromisoformat(d["dateTime"]).date().isoformat()
                    row = daily_by_date.setdefault(day, {})
                    for phen, lv in levels[i].items():
                        row[phen] = max(row.get(phen, 0), lv)
    except (KeyError, TypeError, IndexError, ValueError, AttributeError) as err:
        raise JmaParseError(f"warning timeline: {err!r}") from err
    if not area_seen:
        raise JmaParseError(f"warning timeline: area {area} not in the file")

    steps = [steps_by_start[k] for k in sorted(steps_by_start)]
    daily = [{"date": d, "levels": daily_by_date[d]} for d in sorted(daily_by_date)]
    level = max((lv for s in steps for lv in s["levels"].values()), default=0)
    peak = None
    if level >= 2:
        first = next(s for s in steps if level in s["levels"].values())
        phenomena: list[str] = []
        for s in steps:
            for phen, lv in s["levels"].items():
                if lv == level and phen not in phenomena:
                    phenomena.append(phen)
        peak = {"level": level, "phenomena": phenomena, "start": first["start"]}
    report = data.get("reportDatetime") if isinstance(data, dict) else None
    return WarningOutlook(report, level, steps, daily, peak)
