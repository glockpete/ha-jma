"""Sensors: warning level + outlook, rain next hour + outlook, radar frames (+ map layers),
overview, last earthquake, typhoon, WBGT, キキクル at the configured place, nationwide city forecast."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    JMA_ATTRIBUTION,
    KIKIKURU_ELEMENTS,
    NOWCAST_ZOOM,
    QUAKE_ATTRIBUTION,
    TSUNAMI_GRADE_KEY,
    URL_NOWC_TILE,
    URL_WARNING_PAGE,
    WBGT_ATTRIBUTION,
    WBGT_PREF,
)
from .coordinator import JmaConfigEntry, signal_quake
from .entity import JmaEntity
from .parse_heat_alert import grades
from .parse_layers import layers_attribute
from .parse_nowcast import parse_jma_time
from .parse_tsunami import CONDITION_EN
from .quake_ws import quake_event_data

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: JmaConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    sensors: list[SensorEntity] = [
        WarningLevelSensor(entry),
        RainNextHourSensor(entry),
        RadarFramesSensor(entry),
        OverviewSensor(entry),
        LastEarthquakeSensor(entry),
        TsunamiSensor(entry),
        WarningOutlookSensor(entry),
        TyphoonSensor(entry),
    ]
    # The heat index exists only for offices whose CSV file name is known (WBGT_PREF); for any other
    # office the sensor is not created, rather than left unavailable for ever.
    if entry.runtime_data.cfg.office in WBGT_PREF:
        sensors.append(WbgtSensor(entry))
    sensors += [
        HeatAlertSensor(entry),
        KikikuruSensor(entry),
        RainOutlookSensor(entry),
        JapanSensor(entry),
    ]
    async_add_entities(sensors)


class _CoordSensor(JmaEntity, CoordinatorEntity, SensorEntity):
    _attr_attribution = JMA_ATTRIBUTION  # every sensor of this base shows the agency's data

    def __init__(self, entry: JmaConfigEntry, key: str, coordinator) -> None:
        JmaEntity.__init__(self, entry, key)
        CoordinatorEntity.__init__(self, coordinator)


class WarningLevelSensor(_CoordSensor):
    """0 = nothing in force; 2..5 = highest JMA level for the area."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:alert"

    def __init__(self, entry: JmaConfigEntry) -> None:
        super().__init__(entry, "warning_level", entry.runtime_data.warnings)

    @property
    def native_value(self) -> int | None:
        d = self.coordinator.data
        return d.area.level if d else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self.coordinator.data
        cfg = self.runtime.cfg
        attrs: dict[str, Any] = {
            "area": cfg.area, "area_name": cfg.area_name,
            "jma_url": URL_WARNING_PAGE.format(area=cfg.area),
        }
        if d:
            top = d.area.warnings[0] if d.area.warnings else None
            headline = (d.area.headlines.get(top.data_type) if top else None) or d.area.headline
            attrs.update(
                warnings=[w.as_dict() for w in d.area.warnings],
                headline=headline,
                report_time=d.area.report_time,
                area_missing=d.area.area_missing,
                unknown_codes=d.area.unknown_codes,
            )
        return attrs


class RainNextHourSensor(_CoordSensor):
    """Max rain intensity at the configured place over the next 60 minutes (band upper bound)."""

    _attr_device_class = SensorDeviceClass.PRECIPITATION_INTENSITY
    _attr_native_unit_of_measurement = "mm/h"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset({"series"})

    def __init__(self, entry: JmaConfigEntry) -> None:
        super().__init__(entry, "rain_next_hour", entry.runtime_data.nowcast)

    @property
    def native_value(self) -> float | None:
        d = self.coordinator.data
        return d.rain.max_mmh if d else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self.coordinator.data
        if not d:
            return {}
        return {
            "series": d.rain.series,
            "minutes_to_rain": d.rain.minutes_to_rain,
            "raining_now": d.rain.raining_now,
            "basetime": d.rain.basetime,
        }


class RadarFramesSensor(_CoordSensor):
    """Frame lists for map consumers (targetTimes has no CORS header, so HA serves them).

    `layers` comes from the layers coordinator; the state stays the nowcast basetime.
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:radar"
    _unrecorded_attributes = frozenset({"past", "forecast", "tile_template", "home", "zoom", "layers"})

    def __init__(self, entry: JmaConfigEntry) -> None:
        super().__init__(entry, "radar_frames", entry.runtime_data.nowcast)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self.runtime.layers.async_add_listener(self._handle_coordinator_update))

    @property
    def native_value(self) -> datetime | None:
        d = self.coordinator.data
        return parse_jma_time(d.basetime) if d else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self.coordinator.data
        cfg = self.runtime.cfg
        attrs: dict[str, Any] = {
            "tile_template": URL_NOWC_TILE,
            "home": [cfg.latitude, cfg.longitude],
            "zoom": NOWCAST_ZOOM,
        }
        if d:
            attrs.update(basetime=d.basetime, past=d.past, forecast=d.forecast)
        # A failed coordinator keeps its old .data: only a current poll's frames are served.
        lc = self.runtime.layers
        attrs["layers"] = (
            lc.data.layers if lc.data and lc.last_update_success
            else layers_attribute()  # the static GSI layer only
        )
        return attrs


class OverviewSensor(_CoordSensor):
    """JMA's prose overview for the office (Japanese)."""

    _attr_icon = "mdi:text-box-outline"
    _unrecorded_attributes = frozenset({"text"})

    def __init__(self, entry: JmaConfigEntry) -> None:
        super().__init__(entry, "overview", entry.runtime_data.forecast)

    @property
    def native_value(self) -> str | None:
        d = self.coordinator.data
        if not d or not d.overview:
            return None
        first = d.overview.split("。")[0]
        return (first + "。")[:255] if first else d.overview[:255]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self.coordinator.data
        if not d:
            return {}
        return {
            "text": d.overview,
            "report_time": d.overview_time.isoformat() if d.overview_time else None,
        }


class WarningOutlookSensor(_CoordSensor):
    """Highest warning level (0/2..5) JMA's 3-hourly risk outlook expects for the area."""

    _attr_icon = "mdi:alert-outline"
    _unrecorded_attributes = frozenset({"steps", "daily"})

    def __init__(self, entry: JmaConfigEntry) -> None:
        super().__init__(entry, "warning_outlook", entry.runtime_data.timeline)

    @property
    def native_value(self) -> int | None:
        d = self.coordinator.data
        return d.level if d else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self.coordinator.data
        if not d:
            return {}
        return {"report_time": d.report_time, "steps": d.steps, "daily": d.daily, "peak": d.peak}


class TyphoonSensor(_CoordSensor):
    """Number of active tropical cyclones (0 is a valid state)."""

    _attr_icon = "mdi:weather-hurricane"
    _unrecorded_attributes = frozenset({"storms"})

    def __init__(self, entry: JmaConfigEntry) -> None:
        super().__init__(entry, "typhoon", entry.runtime_data.typhoon)

    @property
    def native_value(self) -> int | None:
        d = self.coordinator.data
        return d.count if d else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self.coordinator.data
        return {"storms": d.storms} if d else {}


class WbgtSensor(_CoordSensor):
    """Today's (JST) highest forecast WBGT (暑さ指数). Unavailable out of season.

    No device class: WBGT is not an air temperature.
    """

    _attr_icon = "mdi:sun-thermometer"
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_attribution = WBGT_ATTRIBUTION
    _unrecorded_attributes = frozenset({"series"})

    def __init__(self, entry: JmaConfigEntry) -> None:
        super().__init__(entry, "wbgt", entry.runtime_data.wbgt)

    @property
    def available(self) -> bool:
        d = self.coordinator.data
        return super().available and d is not None and d.forecast is not None

    @property
    def native_value(self) -> float | None:
        d = self.coordinator.data
        return d.forecast.today_max if d and d.forecast else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self.coordinator.data
        if not d or not d.forecast:
            return {}
        f = d.forecast
        return {"series": f.series, "issued": f.issued, "station": f.station}


class HeatAlertSensor(_CoordSensor):
    """環境省 熱中症警戒アラート for the office's prefecture: today's grade (none / alert / special).

    Unavailable when there is no bulletin (out of season, nothing published or fetched). The state
    is unknown while today is in neither of the bulletin's dates (e.g. just after midnight with
    yesterday's 05:00 file).
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["none", "alert", "special"]
    _attr_icon = "mdi:sun-thermometer-outline"
    _attr_attribution = WBGT_ATTRIBUTION
    _unrecorded_attributes = frozenset({"wbgt"})

    def __init__(self, entry: JmaConfigEntry) -> None:
        super().__init__(entry, "heat_alert", entry.runtime_data.heat_alert)

    @staticmethod
    def _today():
        return dt_util.utcnow().astimezone(timezone(timedelta(hours=9))).date()

    @property
    def available(self) -> bool:
        d = self.coordinator.data
        return super().available and d is not None and d.bulletin is not None

    @property
    def native_value(self) -> str | None:
        d = self.coordinator.data
        return grades(d.bulletin, self._today())["today"] if d and d.bulletin else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self.coordinator.data
        if not d or not d.bulletin:
            return {}
        b, today = d.bulletin, self._today()
        g = grades(b, today)
        covered = {b.target1, b.target2}
        tomorrow = today + timedelta(days=1)
        return {
            "tomorrow": g["tomorrow"],
            "special_judged": g["special_judged"],
            "target_today": today.isoformat() if today in covered else None,
            "target_tomorrow": tomorrow.isoformat() if tomorrow in covered else None,
            "report_time": b.report_time.isoformat(),
            "area": b.area,
            "wbgt_date": b.wbgt_date.isoformat() if b.wbgt_date else None,  # the day the figures are FOR
            "wbgt_home": b.wbgt_home,
            "wbgt_home_station": b.wbgt_home_station,
            "wbgt_max": b.wbgt_max,
            "wbgt": b.wbgt,
        }


class KikikuruSensor(_CoordSensor):
    """キキクル (浸水・土砂・洪水) at the configured place, newest frame: max level 0-5."""

    _attr_icon = "mdi:home-flood"

    def __init__(self, entry: JmaConfigEntry) -> None:
        super().__init__(entry, "kikikuru", entry.runtime_data.layers)

    @property
    def available(self) -> bool:
        d = self.coordinator.data
        return super().available and d is not None and d.kikikuru is not None

    @property
    def native_value(self) -> int | None:
        d = self.coordinator.data
        return d.kikikuru.level if d and d.kikikuru else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self.coordinator.data
        if not d or not d.kikikuru:
            return {}
        k = d.kikikuru
        return {"inund": k.inund, "land": k.land, "flood": k.flood, "basetime": k.basetime,
                # the state is a lower bound while any of these could not be read
                "partial": [name for name in KIKIKURU_ELEMENTS if getattr(k, name) is None]}


class RainOutlookSensor(_CoordSensor):
    """降水短時間予報 at the configured place beyond the nowcast hour: max mm/h (band upper bound)."""

    _attr_device_class = SensorDeviceClass.PRECIPITATION_INTENSITY
    _attr_native_unit_of_measurement = "mm/h"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _unrecorded_attributes = frozenset({"series"})

    def __init__(self, entry: JmaConfigEntry) -> None:
        super().__init__(entry, "rain_outlook", entry.runtime_data.layers)

    @property
    def available(self) -> bool:
        d = self.coordinator.data
        return super().available and d is not None and d.rain_outlook is not None

    @property
    def native_value(self) -> float | None:
        d = self.coordinator.data
        return d.rain_outlook.max_mmh if d and d.rain_outlook else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self.coordinator.data
        if not d or not d.rain_outlook:
            return {}
        r = d.rain_outlook
        return {"series": r.series, "first_rain": r.first_rain, "basetime": r.basetime}


class JapanSensor(_CoordSensor):
    """Nationwide city forecast, today + tomorrow; the state is JMA's newest report time."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:map-outline"
    _unrecorded_attributes = frozenset({"cities"})

    def __init__(self, entry: JmaConfigEntry) -> None:
        super().__init__(entry, "japan", entry.runtime_data.japan)

    @property
    def native_value(self) -> datetime | None:
        d = self.coordinator.data
        return datetime.fromisoformat(d.report_time) if d else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        d = self.coordinator.data
        return {"cities": d.cities} if d else {}


class LastEarthquakeSensor(JmaEntity, SensorEntity):
    """Origin time of the newest quake P2P reported, with the intensity in the prefecture, in the area (the 23
    wards of Tokyo only) and at the configured place (pref_max_scale / pref_shindo, area_max_scale /
    area_shindo, local_scale / local_shindo)."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:waveform"
    _attr_attribution = QUAKE_ATTRIBUTION

    def __init__(self, entry: JmaConfigEntry) -> None:
        super().__init__(entry, "last_earthquake")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(
            self.hass, signal_quake(self._entry_id), self._update))

    @callback
    def _update(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return super().available and self.runtime.quake.enabled

    @property
    def native_value(self) -> datetime | None:
        q = self.runtime.quake.last_quake
        return q.time if q else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        q = self.runtime.quake.last_quake
        attrs = quake_event_data(q) if q else {}
        if q is None:  # before any quake: the entry's own place
            place = self.runtime.quake.place
            attrs["pref"] = place.pref
            attrs["area"] = place.area
            attrs["local_name"] = place.local_name
        attrs["connected"] = self.runtime.quake.connected
        return attrs


class TsunamiSensor(JmaEntity, SensorEntity):
    """Highest tsunami grade in force for the home coast area (none / watch / warning / major).

    Unknown until the first replay of P2P's tsunami list has run; `none` once it has run and
    nothing is in force."""

    _attr_attribution = QUAKE_ATTRIBUTION  # P2P地震情報 relays the agency's tsunami information
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(TSUNAMI_GRADE_KEY)
    _attr_icon = "mdi:tsunami"
    _unrecorded_attributes = frozenset({"areas_major", "areas_warning"})

    def __init__(self, entry: JmaConfigEntry) -> None:
        super().__init__(entry, "tsunami")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(
            self.hass, signal_quake(self._entry_id), self._update))

    @callback
    def _update(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return super().available and self.runtime.quake.enabled

    @property
    def native_value(self) -> str | None:
        tracker = self.runtime.quake.tsunami
        picture = tracker.picture(dt_util.now())
        if picture is None:
            return "none" if tracker.replayed else None
        return picture.home

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        tracker = self.runtime.quake.tsunami
        bulletin = tracker.current
        picture = tracker.picture(dt_util.now())
        if bulletin is None or picture is None:
            return {"home_area": tracker.home_area}
        entry = picture.home_area_entry
        return {
            "home_area": tracker.home_area,
            "max_grade": picture.max,
            "counts": picture.counts,
            "areas_major": picture.areas_major,
            "areas_warning": picture.areas_warning,
            "issued": bulletin.issued.isoformat(),
            "id": bulletin.id,
            "cancelled": bulletin.cancelled,
            "home_height": entry.height if entry else None,
            "home_height_m": entry.height_m if entry else None,
            "home_arrival": entry.arrival.isoformat() if entry and entry.arrival else None,
            "home_condition": CONDITION_EN.get(entry.condition) if entry and entry.condition
            else None,
            "home_immediate": entry.immediate if entry else None,
        }
