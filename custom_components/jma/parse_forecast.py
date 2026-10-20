"""Parse JMA's forecast JSON (/bosai/forecast/data/forecast/{office}.json). No HA imports.

The temps / pops rules copy the JMA forecast page's own JS:
- short-term temps, morning layout (05/11): today {max: temps[0]}, tomorrow {min: temps[2], max: temps[3]};
  evening layout (17, first time already tomorrow): today has none, tomorrow {min: temps[0], max: temps[1]}.
- short-term pops (6-hourly): today = pops[:len-4], tomorrow = pops[len-4:].
- weekly block: index 0 is tomorrow; its blanks come from the short-term block.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date, datetime
from typing import Any

from .errors import JmaParseError
from .telops import TELOPS

HA_CONDITIONS = frozenset({
    "clear-night", "cloudy", "exceptional", "fog", "hail", "lightning", "lightning-rainy",
    "partlycloudy", "pouring", "rainy", "snowy", "snowy-rainy", "sunny", "windy", "windy-variant",
})


@dataclass(frozen=True)
class DayForecast:
    """One forecast day."""

    date: date
    code: str | None
    text_ja: str | None
    pop: int | None
    temp_max: float | None
    temp_min: float | None
    reliability: str | None
    # Weekly days only: JMA's likely range around each figure (tempsMax/MinUpper/Lower).
    temp_max_upper: float | None = None
    temp_max_lower: float | None = None
    temp_min_upper: float | None = None
    temp_min_lower: float | None = None
    # Short-term days only: JMA's wind sentence, e.g. 「東の風 後 南の風」.
    wind_ja: str | None = None


@dataclass(frozen=True)
class ForecastData:
    """The parsed forecast."""

    report_time: datetime
    issue_hour: str
    days: list[DayForecast]
    pops_6h: list[tuple[datetime, int | None]]
    # 平年値 for the forecast station over the weekly period (weekly block `tempAverage`).
    normal_temp_max: float | None = None
    normal_temp_min: float | None = None


def condition_for(code: str | None, is_night: bool = False) -> str:
    """Map a JMA weather code to a Home Assistant condition.

    Base = JMA's own group (100 sunny, 200 cloudy, 300 rain, 400 snow), refined on the
    JAPANESE name. The English labels are inconsistent ("HEAVYRAIN", "MOSTLY CLOUDY" for
    曇時々晴), the Japanese ones are not. First match wins.
    """
    row = TELOPS.get(code or "")
    if row is None:
        return "exceptional"
    group, name = row[2], row[3]
    if "雷" in name and group != "100":  # 晴山沿い雷雨 = clear in the plains
        return "lightning-rainy"
    if "雪" in name and ("雨" in name or "みぞれ" in name):
        return "snowy-rainy"
    if group == "400":
        return "snowy"
    if group == "300":
        return "pouring" if any(w in name for w in ("大雨", "強く", "暴風")) else "rainy"
    if group == "100":
        if "曇" in name:
            return "partlycloudy"
        return "clear-night" if is_night else "sunny"
    if group == "200":
        if "霧" in name:
            return "fog"
        return "partlycloudy" if "晴" in name else "cloudy"
    return "exceptional"


def icon_for(code: str | None, is_night: bool = False) -> str | None:
    """JMA's own SVG file name for a code (day or night variant)."""
    row = TELOPS.get(code or "")
    if row is None:
        return None
    return row[1] if is_night else row[0]


def name_for(code: str | None) -> tuple[str | None, str | None]:
    """(Japanese, English) short names for a code."""
    row = TELOPS.get(code or "")
    return (row[3], row[4].title()) if row else (None, None)


def _num(value: Any) -> float | None:
    """A finite number, else None: JSON 1e400 parses to inf, and "nan" / "inf" strings float() happily."""
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):  # OverflowError: an int too big for a float
        return None
    return number if math.isfinite(number) else None


def _int(value: Any) -> int | None:
    number = _num(value)
    return None if number is None else int(number)


def _row(series: dict, code: str) -> dict:
    """The area row with this code, else the first row (offices name their areas differently)."""
    areas = series["areas"]
    for area in areas:
        if area["area"]["code"] == code:
            return area
    if not areas:
        raise JmaParseError("time series has no areas")
    return areas[0]


def _max_or_none(values: list[int | None]) -> int | None:
    present = [v for v in values if v is not None]
    return max(present) if present else None


def parse_forecast(data: Any, class10: str, station: str) -> ForecastData:
    """Parse the forecast list into days (today first) and the 6-hour rain chances."""
    try:
        short = data[0]
        report_time = datetime.fromisoformat(short["reportDatetime"])
        issue_hour = report_time.strftime("%H")
        ts_weather, ts_pops, ts_temps = short["timeSeries"][:3]

        weather_row = _row(ts_weather, class10)
        days: dict[date, DayForecast] = {}
        for i, when in enumerate(ts_weather["timeDefines"]):
            day = datetime.fromisoformat(when).date()
            text = weather_row.get("weathers", [None] * (i + 1))[i]
            winds = weather_row.get("winds") or []
            wind = winds[i] if i < len(winds) else None
            days[day] = DayForecast(
                date=day,
                code=weather_row["weatherCodes"][i],
                text_ja=text.replace("　", " ") if text else None,
                pop=None, temp_max=None, temp_min=None, reliability=None,
                wind_ja=wind.replace("　", " ") if wind else None,
            )
        ordered = sorted(days)
        today = ordered[0]
        tomorrow = ordered[1] if len(ordered) > 1 else None

        pops_row = _row(ts_pops, class10)
        pops = [_int(p) for p in pops_row["pops"]]
        pop_times = [datetime.fromisoformat(t) for t in ts_pops["timeDefines"]]
        pops_6h = list(zip(pop_times, pops, strict=False))
        split = max(len(pops) - 4, 0)
        days[today] = replace(days[today], pop=_max_or_none(pops[:split]))
        if tomorrow:
            days[tomorrow] = replace(days[tomorrow], pop=_max_or_none(pops[split:]))

        temps = [_num(t) for t in _row(ts_temps, station)["temps"]]
        # Evening layout (only tomorrow's min/max) is recognised by its first time lying after the
        # report date, not by the issue hour: an off-schedule re-issue (随時修正) at 18:10 would
        # otherwise take the morning branch and put tomorrow's min in today's max.
        first_temp_day = datetime.fromisoformat(ts_temps["timeDefines"][0]).date()
        if first_temp_day > report_time.date():
            if tomorrow and len(temps) >= 2:
                days[tomorrow] = replace(days[tomorrow], temp_min=temps[0], temp_max=temps[1])
        else:
            if temps:
                days[today] = replace(days[today], temp_max=temps[0])
            if tomorrow and len(temps) >= 4:
                days[tomorrow] = replace(days[tomorrow], temp_min=temps[2], temp_max=temps[3])

        normal_max = normal_min = None
        if len(data) > 1:
            weekly = data[1]
            wk_weather, wk_temps = weekly["timeSeries"][:2]
            wrow = _row(wk_weather, class10)
            trow = _row(wk_temps, station)

            def col(key: str, i: int) -> float | None:
                values = trow.get(key) or []
                return _num(values[i]) if i < len(values) else None

            for i, when in enumerate(wk_weather["timeDefines"]):
                day = datetime.fromisoformat(when).date()
                code = wrow["weatherCodes"][i]
                pop = _int(wrow.get("pops", [""] * (i + 1))[i])
                rel = wrow.get("reliabilities", [""] * (i + 1))[i] or None
                tmin = col("tempsMin", i)
                tmax = col("tempsMax", i)
                bounds = {
                    "temp_max_upper": col("tempsMaxUpper", i),
                    "temp_max_lower": col("tempsMaxLower", i),
                    "temp_min_upper": col("tempsMinUpper", i),
                    "temp_min_lower": col("tempsMinLower", i),
                }
                have = days.get(day)
                if have is None:
                    days[day] = DayForecast(
                        day, code, None, pop, tmax, tmin, rel,
                        temp_max_upper=bounds["temp_max_upper"], temp_max_lower=bounds["temp_max_lower"],
                        temp_min_upper=bounds["temp_min_upper"], temp_min_lower=bounds["temp_min_lower"],
                    )
                else:
                    # Short-term wins where it has a value; weekly fills the blanks. The bounds
                    # belong to the weekly figures, so they are kept only where weekly supplied it.
                    days[day] = replace(
                        have,
                        pop=have.pop if have.pop is not None else pop,
                        temp_max=have.temp_max if have.temp_max is not None else tmax,
                        temp_min=have.temp_min if have.temp_min is not None else tmin,
                        reliability=have.reliability or rel,
                        temp_max_upper=bounds["temp_max_upper"] if have.temp_max is None else None,
                        temp_max_lower=bounds["temp_max_lower"] if have.temp_max is None else None,
                        temp_min_upper=bounds["temp_min_upper"] if have.temp_min is None else None,
                        temp_min_lower=bounds["temp_min_lower"] if have.temp_min is None else None,
                    )
            for area in (weekly.get("tempAverage") or {}).get("areas") or []:
                if (area.get("area") or {}).get("code") == station:
                    normal_min, normal_max = _num(area.get("min")), _num(area.get("max"))
    except JmaParseError:
        raise
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as err:
        raise JmaParseError(f"forecast: {err!r}") from err

    return ForecastData(
        report_time=report_time,
        issue_hour=issue_hour,
        days=[days[d] for d in sorted(days)],
        pops_6h=pops_6h,
        normal_temp_max=normal_max,
        normal_temp_min=normal_min,
    )


def parse_overview(data: Any) -> tuple[str, datetime]:
    """(text, report time) from overview_forecast; full-width spaces trimmed."""
    try:
        text = "\n".join(
            line.strip("　 ") for line in str(data["text"]).split("\n")
        ).strip()
        return text, datetime.fromisoformat(data["reportDatetime"])
    except (KeyError, TypeError, ValueError) as err:
        raise JmaParseError(f"overview: {err!r}") from err
