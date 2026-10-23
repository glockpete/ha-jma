"""Parse Open-Meteo's JMA endpoint (models=jma_msm) into hourly forecast rows. No HA imports at run time:
HA's `Forecast` is only the type of a row."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal

from .errors import JmaParseError

if TYPE_CHECKING:
    from homeassistant.components.weather import Forecast

HOURLY_VARS = (
    "temperature_2m",
    "apparent_temperature",
    "precipitation",
    "precipitation_probability",
    "weather_code",
    "wind_speed_10m",
    "wind_direction_10m",
    "relative_humidity_2m",
    "cloud_cover",
    "dew_point_2m",
    "is_day",
)
# Note: all-null for JMA models (msm and seamless): uv_index, visibility,
# wind_gusts_10m, cape, and precipitation_probability (kept above; it is simply left out).

# variable -> HA Forecast key. The keys are Literals so that `row[key]` is a checked Forecast key.
type ForecastKey = Literal[
    "native_temperature", "native_apparent_temperature", "native_precipitation", "precipitation_probability",
    "native_wind_speed", "wind_bearing", "humidity", "cloud_coverage", "native_dew_point",
]
KEYS: dict[str, ForecastKey] = {
    "temperature_2m": "native_temperature",
    "apparent_temperature": "native_apparent_temperature",
    "precipitation": "native_precipitation",
    "precipitation_probability": "precipitation_probability",
    "wind_speed_10m": "native_wind_speed",
    "wind_direction_10m": "wind_bearing",
    "relative_humidity_2m": "humidity",
    "cloud_cover": "cloud_coverage",
    "dew_point_2m": "native_dew_point",
}


def open_meteo_params(lat: float, lon: float) -> dict[str, str]:
    return {
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "hourly": ",".join(HOURLY_VARS),
        "models": "jma_msm",
        "timezone": "Asia/Tokyo",
        "wind_speed_unit": "ms",
        "forecast_days": "3",
    }


def wmo_condition(code: int | None, is_day: bool) -> str | None:
    """WMO weather code -> HA condition (same table HA's own open_meteo integration uses)."""
    if code is None:
        return None
    if code == 0:
        return "sunny" if is_day else "clear-night"
    if code in (1, 2):
        return "partlycloudy"
    if code == 3:
        return "cloudy"
    if code in (45, 48):
        return "fog"
    if code in (65, 67, 82):
        return "pouring"
    if 51 <= code <= 67 or 80 <= code <= 82:
        return "rainy"
    if 71 <= code <= 77 or code in (85, 86):
        return "snowy"
    if code in (95, 96, 99):
        return "lightning-rainy"
    return None


def parse_hourly(data: Any) -> list[Forecast]:
    """Rows with HA Forecast keys; variables that are null for every hour are left out."""
    try:
        hourly = data["hourly"]
        tz = timezone(timedelta(seconds=int(data.get("utc_offset_seconds", 32400))))
        times = hourly["time"]
    except (KeyError, TypeError, ValueError) as err:
        raise JmaParseError(f"open-meteo: {err!r}") from err
    present = {
        var for var in HOURLY_VARS
        if any(v is not None for v in hourly.get(var) or [])
    }
    def at(var: str, i: int, default: Any = None) -> Any:
        values = hourly.get(var) or []
        return values[i] if i < len(values) else default  # a ragged array must not raise

    rows: list[Forecast] = []
    for i, when in enumerate(times):
        row: Forecast = {"datetime": datetime.fromisoformat(when).replace(tzinfo=tz).isoformat()}
        for var, key in KEYS.items():
            if var in present:
                row[key] = at(var, i)
        code = at("weather_code", i) if "weather_code" in present else None
        day = at("is_day", i, 1) if "is_day" in present else 1
        condition = wmo_condition(code, bool(day))
        if condition:
            row["condition"] = condition
        if row.get("native_temperature") is None:
            continue  # beyond the model's horizon
        rows.append(row)
    return rows
