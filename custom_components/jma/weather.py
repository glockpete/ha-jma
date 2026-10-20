"""weather.jma_<place>: AMeDAS now, JMA daily forecast, Open-Meteo JMA MSM hourly."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from homeassistant.components.weather import Forecast, WeatherEntity, WeatherEntityFeature
from homeassistant.const import (
    UnitOfPrecipitationDepth,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from .const import ATTRIBUTION
from .coordinator import JmaConfigEntry
from .entity import JmaEntity
from .parse_forecast import DayForecast, condition_for, icon_for, name_for

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: JmaConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([JmaWeather(entry)])


class JmaWeather(JmaEntity, WeatherEntity):
    _attr_name = None
    _attr_attribution = ATTRIBUTION
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_native_pressure_unit = UnitOfPressure.HPA
    _attr_native_wind_speed_unit = UnitOfSpeed.METERS_PER_SECOND
    _attr_native_precipitation_unit = UnitOfPrecipitationDepth.MILLIMETERS
    _attr_supported_features = (
        WeatherEntityFeature.FORECAST_DAILY | WeatherEntityFeature.FORECAST_HOURLY
    )

    def __init__(self, entry: JmaConfigEntry) -> None:
        super().__init__(entry, "weather")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        rt = self.runtime
        for coordinator in (rt.amedas, rt.nowcast):
            self.async_on_remove(coordinator.async_add_listener(self._write))
        self.async_on_remove(rt.forecast.async_add_listener(self._daily_changed))
        self.async_on_remove(rt.hourly.async_add_listener(self._hourly_changed))
        # day/night (clear-night vs sunny, the night icon) follows sun.sun at the moment it crosses the
        # horizon, not at the next coordinator update
        self.async_on_remove(async_track_state_change_event(self.hass, ["sun.sun"], self._sun_changed))

    @callback
    def _write(self) -> None:
        self.async_write_ha_state()

    @callback
    def _sun_changed(self, event: Event[EventStateChangedData]) -> None:
        old, new = event.data["old_state"], event.data["new_state"]
        # sun.sun rewrites its elevation/azimuth all day: only a horizon crossing changes this entity
        if (old.state if old else None) != (new.state if new else None):
            self.async_write_ha_state()

    @callback
    def _daily_changed(self) -> None:
        self.async_write_ha_state()
        self.hass.async_create_task(self.async_update_listeners(["daily"]))

    @callback
    def _hourly_changed(self) -> None:
        self.async_write_ha_state()
        self.hass.async_create_task(self.async_update_listeners(["hourly"]))

    # ---- helpers ----

    @property
    def _obs(self):
        a = self.runtime.amedas
        return a.data if a.last_update_success else None

    def _is_night(self) -> bool:
        sun = self.hass.states.get("sun.sun")
        return bool(sun and sun.state == "below_horizon")

    def _today(self) -> DayForecast | None:
        f = self.runtime.forecast
        if not f.last_update_success or f.data is None:
            return None
        today = dt_util.now().date()
        for day in f.data.forecast.days:
            if day.date >= today:
                return day
        return None

    def _current_hour(self) -> dict[str, Any] | None:
        h = self.runtime.hourly
        if not h.last_update_success or not h.data:
            return None
        start = dt_util.now().replace(minute=0, second=0, microsecond=0)
        for row in h.data:
            if datetime.fromisoformat(row["datetime"]) >= start:
                return row
        return None

    # ---- state ----

    @property
    def available(self) -> bool:
        return self._obs is not None or self._today() is not None

    @property
    def condition(self) -> str | None:
        n = self.runtime.nowcast
        if n.last_update_success and n.data and n.data.wet:
            band = n.data.rain.series[0]["band"] or 0
            return "pouring" if band >= 5 else "rainy"
        today = self._today()
        return condition_for(today.code, self._is_night()) if today else None

    @property
    def native_temperature(self) -> float | None:
        return self._obs.temperature if self._obs else None

    @property
    def native_apparent_temperature(self) -> float | None:
        row = self._current_hour()
        return row.get("native_apparent_temperature") if row else None

    @property
    def humidity(self) -> float | None:
        return self._obs.humidity if self._obs else None

    @property
    def native_pressure(self) -> float | None:
        return self._obs.pressure if self._obs else None

    @property
    def native_wind_speed(self) -> float | None:
        return self._obs.wind_speed if self._obs else None

    # No native_wind_gust_speed: AMeDAS only reports the day's MAXIMUM gust, which is exposed as
    # gust_max_today (m/s) + gust_max_time instead of posing as the current gust.

    @property
    def wind_bearing(self) -> float | None:
        return self._obs.wind_bearing if self._obs else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        rt = self.runtime
        today = self._today()
        night = self._is_night()
        names = rt.extra.get("station_names") or {}
        attrs: dict[str, Any] = {
            "station_primary": rt.cfg.station_primary,
            "station_secondary": rt.cfg.station_secondary,
            "station_primary_name": names.get(rt.cfg.station_primary),
            "station_secondary_name": names.get(rt.cfg.station_secondary),
        }
        if today:
            ja, en = name_for(today.code)
            attrs.update(jma_code=today.code, jma_icon=icon_for(today.code, night),
                         jma_name_ja=ja, jma_name_en=en, jma_text=today.text_ja,
                         temp_max=today.temp_max, temp_min=today.temp_min,
                         wind_text=today.wind_ja)
        if rt.forecast.data:
            fc = rt.forecast.data.forecast
            attrs["pops_6h"] = [{"time": t.isoformat(), "pop": p} for t, p in fc.pops_6h]
            attrs["forecast_report_time"] = fc.report_time.isoformat()
            attrs.update(normal_temp_max=fc.normal_temp_max, normal_temp_min=fc.normal_temp_min)
        obs = self._obs
        if obs:

            def iso(value: datetime | None) -> str | None:
                return value.isoformat() if value else None

            today_local = dt_util.now().date()

            def todays(value: float | None, at: datetime | None) -> tuple[float | None, str | None]:
                # The 00:00 row closes YESTERDAY (24:00): its extremes are not "today" (the feed repeats the previous day's extremes shortly after midnight).
                if value is None or at is None or dt_util.as_local(at).date() != today_local:
                    return None, None
                return value, at.isoformat()

            gust, gust_at = todays(obs.gust_max, obs.gust_max_time)
            tmax, tmax_at = todays(obs.temp_max_observed, obs.temp_max_observed_time)
            tmin, tmin_at = todays(obs.temp_min_observed, obs.temp_min_observed_time)
            attrs.update(
                observed_at=iso(obs.observed_at),
                precip_10m=obs.precip_10m, precip_1h=obs.precip_1h, precip_24h=obs.precip_24h,
                sun_1h=obs.sun_1h,
                gust_max_today=gust, gust_max_time=gust_at,
                temp_max_observed=tmax, temp_max_observed_time=tmax_at,
                temp_min_observed=tmin, temp_min_observed_time=tmin_at,
            )
        return attrs

    # ---- forecasts ----

    async def async_forecast_daily(self) -> list[Forecast] | None:
        f = self.runtime.forecast
        # A failed coordinator keeps its old .data; serve only what _today() would.
        if not f.last_update_success or f.data is None:
            return None
        tz = dt_util.get_default_time_zone()
        today: date = dt_util.now().date()
        rows: list[Forecast] = []
        for day in f.data.forecast.days:
            if day.date < today:
                continue
            row: dict[str, Any] = {
                "datetime": datetime.combine(day.date, time(0), tz).isoformat(),
                "condition": condition_for(day.code),
                "native_temperature": day.temp_max,
                "native_templow": day.temp_min,
                "precipitation_probability": day.pop,
                "jma_code": day.code,
                "jma_icon": icon_for(day.code),
                "reliability": day.reliability,
            }
            for key in ("temp_max_upper", "temp_max_lower", "temp_min_upper", "temp_min_lower"):
                if getattr(day, key) is not None:
                    row[key] = getattr(day, key)
            rows.append(row)  # type: ignore[arg-type]
        return rows

    async def async_forecast_hourly(self) -> list[Forecast] | None:
        h = self.runtime.hourly
        if not h.last_update_success or not h.data:
            return None
        start = dt_util.now().replace(minute=0, second=0, microsecond=0)
        return [
            row for row in h.data  # type: ignore[misc]
            if datetime.fromisoformat(row["datetime"]) >= start
        ]
