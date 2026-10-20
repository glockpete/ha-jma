"""Parse AMeDAS point files (/bosai/amedas/data/point/{id}/{YYYYMMDD}_{HH}.json). No HA imports.

Each file holds one 3-hour JST block keyed by "YYYYMMDDHHMMSS"; each element is
[value, quality_flag] and only flag 0 is a usable reading.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, fields, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from .errors import JmaParseError

JST = timezone(timedelta(hours=9))

# element -> Observation field
ELEMENTS = {
    "temp": "temperature",
    "humidity": "humidity",
    "normalPressure": "pressure",
    "wind": "wind_speed",
    "windDirection": "wind_index",
    "precipitation10m": "precip_10m",
    "precipitation1h": "precip_1h",
    "precipitation24h": "precip_24h",
    "sun1h": "sun_1h",
}
# Daily extremes so far (JST day), each with its time element. Note: `gust` is the day's MAXIMUM gust,
# not the current one (the same value and gustTime repeat in every row of the day),
# so it is never the entity's wind_gust_speed. The *Time elements are {hour, minute} in UTC.
EXTREMES = {
    "gust": ("gust_max", "gustTime"),
    "maxTemp": ("temp_max_observed", "maxTempTime"),
    "minTemp": ("temp_min_observed", "minTempTime"),
}
# A reading older than the newest row by more than this is not "now": the other station fills in.
MAX_ROW_LAG = timedelta(minutes=20)


@dataclass(frozen=True)
class Observation:
    """One station's newest usable readings (None where missing or flagged)."""

    station: str | None = None
    observed_at: datetime | None = None
    temperature: float | None = None
    humidity: float | None = None
    pressure: float | None = None
    wind_speed: float | None = None
    wind_index: int | None = None
    precip_10m: float | None = None
    precip_1h: float | None = None
    precip_24h: float | None = None
    sun_1h: float | None = None
    gust_max: float | None = None
    gust_max_time: datetime | None = None
    temp_max_observed: float | None = None
    temp_max_observed_time: datetime | None = None
    temp_min_observed: float | None = None
    temp_min_observed_time: datetime | None = None

    @property
    def wind_bearing(self) -> float | None:
        """JMA 16-point index (1 = NNE ... 16 = N, 0 = calm) to degrees."""
        if self.wind_index in (None, 0):
            return None
        return (self.wind_index * 22.5) % 360


def station_names_from_table(table: Any, station_ids: Iterable[str]) -> tuple[str | None, ...]:
    """The kanji name (kjName) of each station id in amedastable.json, in the order given; None where the
    table has no such station or no usable name. A table that is not an object raises AttributeError."""
    return tuple(
        name if isinstance(name := (table.get(sid) or {}).get("kjName"), str) and name else None
        for sid in station_ids)


def parse_latest_time(text: str) -> datetime:
    try:
        return datetime.fromisoformat(text.strip())
    except ValueError as err:
        raise JmaParseError(f"amedas latest_time: {text[:40]!r}") from err


def point_file_parts(latest: datetime) -> tuple[str, str]:
    """(YYYYMMDD, HH) of the 3-hour JST block holding `latest`."""
    local = latest.astimezone(JST)
    return local.strftime("%Y%m%d"), f"{local.hour // 3 * 3:02d}"


def extreme_time(row_time: datetime, hm: Any) -> datetime | None:
    """A {hour, minute} UTC time-of-day inside the JST day of `row_time`, as a JST datetime."""
    if not isinstance(hm, dict) or not isinstance(hm.get("hour"), int) or not isinstance(hm.get("minute"), int):
        return None
    day_start = row_time.astimezone(JST).replace(hour=0, minute=0, second=0, microsecond=0)
    # The JST day starts at 15:00 UTC of the day before: hours 15-23 UTC fall in its first 9 h.
    offset = timedelta(hours=(hm["hour"] + 9) % 24, minutes=hm["minute"])
    at = day_start + offset
    # The 00:00 row closes the previous day (24:00), so its extremes can lie "tomorrow" by this sum.
    return at - timedelta(days=1) if at > row_time else at


def parse_point(data: Any, station: str) -> Observation:
    """Newest flag-0 value of each element, from rows within MAX_ROW_LAG of the newest row."""
    if not isinstance(data, dict) or not data:
        raise JmaParseError("amedas point: expected a non-empty object")
    values: dict[str, Any] = {}
    newest: datetime | None = None
    first_row: datetime | None = None
    try:
        for key in sorted(data, reverse=True):
            row = data[key]
            when = datetime.strptime(key, "%Y%m%d%H%M%S").replace(tzinfo=JST)
            if first_row is None:
                first_row = when
            elif first_row - when > MAX_ROW_LAG:
                break
            for element, field_name in ELEMENTS.items():
                if field_name in values or element not in row:
                    continue
                value, flag = row[element][0], row[element][1]
                if flag == 0 and value is not None:
                    values[field_name] = value
                    if newest is None or when > newest:
                        newest = when
            for element, (field_name, time_element) in EXTREMES.items():
                if field_name in values or element not in row:
                    continue
                value, flag = row[element][0], row[element][1]
                if flag == 0 and value is not None:
                    values[field_name] = value
                    values[f"{field_name}_time"] = extreme_time(when, row.get(time_element))
    except (KeyError, TypeError, ValueError, IndexError) as err:
        raise JmaParseError(f"amedas point: {err!r}") from err
    return Observation(station=station, observed_at=newest, **values)


def merge_observations(primary: Observation | None, secondary: Observation | None) -> Observation:
    """Primary (nearest) station first, secondary (type-A) fills what it lacks, e.g. pressure."""
    if primary is None and secondary is None:
        return Observation()
    if primary is None:
        return secondary  # type: ignore[return-value]
    if secondary is None:
        return primary
    merged = {
        f.name: getattr(primary, f.name)
        if getattr(primary, f.name) is not None
        else getattr(secondary, f.name)
        for f in fields(Observation)
    }
    # An extreme and its time always come from the same station.
    for field_name, _ in EXTREMES.values():
        source = primary if getattr(primary, field_name) is not None else secondary
        merged[f"{field_name}_time"] = getattr(source, f"{field_name}_time")
    return replace(primary, **merged)
