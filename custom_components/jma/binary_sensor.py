"""binary_sensor.jma_<place>_feed_problem. On once any feed has been bad for 30 min."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    HEALTH_AMEDAS_MAX_AGE,
    HEALTH_FORECAST_MAX_AGE,
    HEALTH_HOURLY_MAX_AGE,
    HEALTH_NOWCAST_MAX_AGE,
    HEALTH_QUAKE_MAX_DOWN,
)
from .coordinator import JmaConfigEntry, JmaRuntime
from .entity import JmaEntity
from .health import HealthTracker
from .parse_nowcast import parse_jma_time

PARALLEL_UPDATES = 0
WARNINGS_MAX_SILENCE = timedelta(minutes=10)
XML_MAX_SILENCE = timedelta(minutes=30)


async def async_setup_entry(
    hass: HomeAssistant, entry: JmaConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([FeedProblem(entry)])


def feed_conditions(rt: JmaRuntime, now: datetime) -> dict[str, str | None]:
    """feed -> reason when unhealthy right now, else None."""
    out: dict[str, str | None] = {}

    w = rt.warnings
    if w.last_ok is None or now - w.last_ok > WARNINGS_MAX_SILENCE:
        out["warnings"] = f"r8 warnings not fetched since {w.last_ok}: {w.last_error}"
    elif w.data and w.data.stale:
        out["warnings"] = f"r8 JSON behind the XML feed for {', '.join(w.data.stale)}"
    elif w.xml_ok is None or now - w.xml_ok > XML_MAX_SILENCE:
        # Without extra.xml the r8-freeze check (the older warning file stops updating) is off; say so.
        out["warnings"] = f"XML cross-check not fetched since {w.xml_ok}: {w.xml_error}"
    else:
        out["warnings"] = None

    obs = rt.amedas.data
    if obs is None or obs.observed_at is None or now - obs.observed_at > HEALTH_AMEDAS_MAX_AGE:
        out["amedas"] = f"no AMeDAS observation newer than {HEALTH_AMEDAS_MAX_AGE}"
    else:
        out["amedas"] = None

    # One station down for days is invisible above: the other one fills every value.
    a = rt.amedas
    out["amedas_station"] = None
    if a.cfg.station_primary != a.cfg.station_secondary:
        pair = (a.cfg.station_primary, a.cfg.station_secondary)
        for sid, other in (pair, pair[::-1]):
            since = a.station_ok.get(sid) or a.first_try
            other_ok = a.station_ok.get(other)
            if (since is not None and now - since > HEALTH_AMEDAS_MAX_AGE
                    and other_ok is not None and now - other_ok <= HEALTH_AMEDAS_MAX_AGE):
                out["amedas_station"] = (
                    f"AMeDAS {sid} has answered nothing since {since.isoformat()} while {other} "
                    f"answers: {a.station_error.get(sid)}")
                break

    h = rt.hourly
    since = h.last_ok or h.first_try
    if since is not None and now - since > HEALTH_HOURLY_MAX_AGE:
        out["hourly"] = f"Open-Meteo hourly not fetched since {h.last_ok}: {h.last_error}"
    else:
        out["hourly"] = None

    nc = rt.nowcast.data
    if nc is None or now - parse_jma_time(nc.basetime) > HEALTH_NOWCAST_MAX_AGE:
        out["nowcast"] = f"no nowcast frame newer than {HEALTH_NOWCAST_MAX_AGE}"
    else:
        out["nowcast"] = None

    fc = rt.forecast.data
    if fc is None or now - fc.forecast.report_time > HEALTH_FORECAST_MAX_AGE:
        out["forecast"] = f"no forecast issued in {HEALTH_FORECAST_MAX_AGE}"
    else:
        out["forecast"] = None

    q = rt.quake
    if q.enabled and not q.connected and q.down_since and now - q.down_since > HEALTH_QUAKE_MAX_DOWN:
        out["quake"] = f"P2P socket down since {q.down_since.isoformat()}"
    else:
        out["quake"] = None
    return out


class FeedProblem(JmaEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC  # about the integration's own health, not the weather

    def __init__(self, entry: JmaConfigEntry) -> None:
        super().__init__(entry, "feed_problem")
        self._tracker = HealthTracker()
        self._state: dict[str, dict] = {}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_interval(self.hass, self._tick, timedelta(minutes=1)))
        for coordinator in self.runtime.coordinators:
            self.async_on_remove(coordinator.async_add_listener(self._refresh))
        self._refresh()

    @callback
    def _tick(self, _now: datetime) -> None:
        self._refresh()

    @callback
    def _refresh(self) -> None:
        self._state = self._tracker.evaluate(
            dt_util.utcnow(), feed_conditions(self.runtime, dt_util.utcnow()))
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        return any(v["problem"] for v in self._state.values())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._state
