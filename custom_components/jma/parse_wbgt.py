"""Parse the 環境省 WBGT (暑さ指数) forecast CSV for one WBGT point. No HA imports.

https://www.wbgt.env.go.jp/prev15WG/dl/yohou_{pref}.csv: a header row
`,,2026092724,2026092803,...` (YYYYMMDDHH, 3-hourly, HH 03..24) and one row per point:
`44132,2026/09/27 22:25, 200, 210, ...` (point number, issued, values x10). Outside the season the
file may be missing (404) or empty, or it keeps the last issue of the season; that is "no data", not an error.

Which file and which point an entry reads is decided here too: `pref_file_for_office` (the file of the office's
prefecture) and `nearest_points` (the points of that file, nearest to the entry's place first; the positions are
the Ministry's own point master, wbgt_points.py).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .const import WBGT_NEAREST_POINTS, WBGT_PREF_FILE
from .parse_typhoon import haversine_km
from .wbgt_points import WBGT_POINTS

JST = timezone(timedelta(hours=9))


@dataclass(frozen=True)
class WbgtPoint:
    number: str  # the point number: the AMeDAS station number, the row key of the file
    name: str  # 観測所名 as the Ministry writes it
    km: float  # from the entry's place


def pref_file_for_office(office: Any) -> str | None:
    """The prefecture file of a forecast office: its first two digits are the prefecture number. None for a
    value that is not an office code."""
    return WBGT_PREF_FILE.get(office[:2]) if isinstance(office, str) else None


def nearest_points(pref: str | None, lat: float, lon: float, count: int = WBGT_NEAREST_POINTS) -> tuple[WbgtPoint, ...]:
    """The `count` points of the prefecture file nearest to (lat, lon), nearest first; empty for an unknown file."""
    ranked = sorted(
        (haversine_km(lat, lon, plat, plon), number, name)
        for number, name, plat, plon in WBGT_POINTS.get(pref or "", ()))
    return tuple(WbgtPoint(number, name, round(km, 1)) for km, number, name in ranked[:count])


@dataclass(frozen=True)
class WbgtForecast:
    station: str
    issued: str | None
    series: list[dict[str, Any]]  # [{time (ISO JST), wbgt (°C) or None}]
    today_max: float | None


def _slot(label: str) -> tuple[date, datetime]:
    """(the CSV's own date, the instant). Note: HH runs 03..24: "2026092724" is 00:00 JST of the next day
    but belongs to the column of the day before (the site shows 24時 as that day's last slot)."""
    day = datetime.strptime(label[:8], "%Y%m%d").replace(tzinfo=JST)
    return day.date(), day + timedelta(hours=int(label[8:10]))


def _value(cell: str) -> float | None:
    try:
        return int(cell.strip()) / 10
    except (ValueError, OverflowError):  # OverflowError: a digit string too long for a float
        return None


def parse_wbgt(text: str | None, stations: tuple[str, ...], today: date) -> WbgtForecast | None:
    """The first listed station present in the CSV; None when there is no usable data."""
    if not text:
        return None
    rows = [[c.strip() for c in line.split(",")] for line in text.splitlines() if line.strip()]
    if len(rows) < 2 or len(rows[0]) < 3:
        return None
    header = rows[0]
    try:
        slots = [_slot(label) for label in header[2:]]
    except ValueError:
        return None  # an HTML page or another format
    by_station = {r[0]: r for r in rows[1:] if r}
    row = next((by_station[s] for s in stations if s in by_station), None)
    if row is None:
        return None
    series, today_vals = [], []
    for (day, when), cell in zip(slots, row[2:], strict=False):
        v = _value(cell)
        series.append({"time": when.isoformat(), "wbgt": v})
        if day == today and v is not None:
            today_vals.append(v)
    if all(s["wbgt"] is None for s in series):
        return None
    issued = None
    if len(row) > 1 and row[1]:
        try:
            issued = datetime.strptime(row[1], "%Y/%m/%d %H:%M").replace(tzinfo=JST).isoformat()
        except ValueError:
            issued = None
    return WbgtForecast(row[0], issued, series, max(today_vals) if today_vals else None)
