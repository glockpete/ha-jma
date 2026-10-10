"""Parse JMA's nationwide city forecast (/bosai/forecast/data/forecast/010000.json). No HA imports.

A list of 22 offices, each {officeCode, name, srf, week}. `srf` and `week` are the per-office
file's two blocks, except that every `areas` is ONE object, not a list. They
are wrapped into lists so parse_forecast's own rules (05/11/17 layouts, weekly fill-in) apply as is.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from .const import JAPAN_CITIES
from .errors import JmaParseError
from .parse_forecast import condition_for, icon_for, parse_forecast

_LOGGER = logging.getLogger(__name__)
_logged: set[str] = set()


@dataclass(frozen=True)
class JapanForecast:
    """Today and tomorrow for each mapped city, in the file's order."""

    report_time: str
    cities: list[dict[str, Any]]


def _log_once(key: str, message: str, *args: Any) -> None:
    if key not in _logged:
        _logged.add(key)
        _LOGGER.warning(message, *args)


def _wrap(part: dict) -> dict:
    areas = part.get("areas")
    return {**part, "areas": [areas]} if isinstance(areas, dict) else part


def _listify(block: dict) -> dict:
    """Shallow copy of an srf / week block with each `areas` object wrapped in a list."""
    out = {**block, "timeSeries": [_wrap(ts) for ts in block["timeSeries"]]}
    for key in ("tempAverage", "precipAverage"):
        if isinstance(block.get(key), dict):
            out[key] = _wrap(block[key])
    return out


def _int(value: float | None) -> int | None:
    return None if value is None else int(round(value))


def _city(office: dict, today: date) -> tuple[dict[str, Any], datetime]:
    name_en, lat, lon = JAPAN_CITIES[office["officeCode"]]
    blocks = [_listify(office["srf"])]
    if isinstance(office.get("week"), dict):
        blocks.append(_listify(office["week"]))
    # One area per block, so parse_forecast's area lookup falls back to it whatever the code.
    fc = parse_forecast(blocks, "", "")
    days = []
    for day in fc.days:
        if day.date not in (today, today + timedelta(days=1)):
            continue
        pops = [p for t, p in fc.pops_6h if t.date() == day.date and p is not None]
        days.append({
            "date": day.date.isoformat(),
            "code": day.code,
            "text": " ".join(day.text_ja.split()) if day.text_ja else None,
            "condition": condition_for(day.code),
            "icon": icon_for(day.code),
            "pop": max(pops) if pops else None,
            "temp_max": _int(day.temp_max),
            "temp_min": _int(day.temp_min),
        })
    city = {
        "office": office["officeCode"], "name": office.get("name"), "name_en": name_en,
        "lat": lat, "lon": lon, "days": days,
    }
    return city, fc.report_time


def parse_japan(data: Any, today: date) -> JapanForecast:
    """Cities with a coordinate in JAPAN_CITIES. One bad office is skipped (logged once); none
    usable is a JmaParseError. `today` is the Asia/Tokyo date."""
    if not isinstance(data, list) or not data:
        raise JmaParseError("japan forecast: expected a non-empty list of offices")
    cities: list[dict[str, Any]] = []
    newest: datetime | None = None
    for office in data:
        code = office.get("officeCode") if isinstance(office, dict) else None
        if code not in JAPAN_CITIES:
            _log_once(f"unmapped:{code}", "japan forecast: office %r has no coordinate, left out", code)
            continue
        try:
            city, report = _city(office, today)
        except (JmaParseError, KeyError, IndexError, TypeError, ValueError, AttributeError) as err:
            _log_once(f"bad:{code}", "japan forecast: office %s skipped: %r", code, err)
            continue
        cities.append(city)
        if newest is None or report > newest:
            newest = report
    if not cities or newest is None:
        raise JmaParseError("japan forecast: no office could be parsed")
    return JapanForecast(report_time=newest.isoformat(), cities=cities)
