"""binary_sensor.jma_<place>_feed_problem. On once any feed has been bad for 30 min."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
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


def silent_station(rt: JmaRuntime, now: datetime) -> tuple[str, str, datetime] | None:
    """(silent station id, the station that answers, silent since) when one of the two AMeDAS stations has answered
    nothing for longer than HEALTH_AMEDAS_MAX_AGE while the other one answers, else None."""
    a = rt.amedas
    if a.cfg.station_primary == a.cfg.station_secondary:
        return None
    pair = (a.cfg.station_primary, a.cfg.station_secondary)
    for sid, other in (pair, pair[::-1]):
        since = a.station_ok.get(sid) or a.first_try
        other_ok = a.station_ok.get(other)
        if (since is not None and now - since > HEALTH_AMEDAS_MAX_AGE
                and other_ok is not None and now - other_ok <= HEALTH_AMEDAS_MAX_AGE):
            return sid, other, since
    return None


ERROR_TEXT_MAX = 200


def clip_error(error: object) -> str:
    """An error text as it goes into a reason: the reasons are attributes of the entity, written to the recorder every
    minute, and a stored error (a server's whole HTML page, say) has no length of its own."""
    text = str(error)
    return text if len(text) <= ERROR_TEXT_MAX else text[:ERROR_TEXT_MAX - 1] + "…"


def feed_conditions(rt: JmaRuntime, now: datetime) -> dict[str, str | None]:
    """feed -> reason when unhealthy right now, else None."""
    out: dict[str, str | None] = {}

    w = rt.warnings
    if w.last_ok is None or now - w.last_ok > WARNINGS_MAX_SILENCE:
        out["warnings"] = f"r8 warnings not fetched since {w.last_ok}: {clip_error(w.last_error)}"
    elif w.data and w.data.stale:
        out["warnings"] = f"r8 JSON behind the XML feed for {', '.join(w.data.stale)}"
    elif w.xml_ok is None or now - w.xml_ok > XML_MAX_SILENCE:
        # Without extra.xml the r8-freeze check (the older warning file stops updating) is off; say so.
        out["warnings"] = f"XML cross-check not fetched since {w.xml_ok}: {clip_error(w.xml_error)}"
    else:
        out["warnings"] = None

    obs = rt.amedas.data
    if obs is None or obs.observed_at is None or now - obs.observed_at > HEALTH_AMEDAS_MAX_AGE:
        out["amedas"] = f"no AMeDAS observation newer than {HEALTH_AMEDAS_MAX_AGE}"
    else:
        out["amedas"] = None

    # One station down for days is invisible above: the other one fills every value.
    out["amedas_station"] = None
    if (silent := silent_station(rt, now)) is not None:
        sid, other, since = silent
        out["amedas_station"] = (
            f"AMeDAS {sid} has answered nothing since {since.isoformat()} while {other} "
            f"answers: {clip_error(rt.amedas.station_error.get(sid))}")

    h = rt.hourly
    hourly_since = h.last_ok or h.first_try
    if hourly_since is not None and now - hourly_since > HEALTH_HOURLY_MAX_AGE:
        out["hourly"] = f"Open-Meteo hourly not fetched since {h.last_ok}: {clip_error(h.last_error)}"
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
    if q.enabled and q.down_since and now - q.down_since > HEALTH_QUAKE_MAX_DOWN:
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
        self._state: dict[str, dict[str, Any]] = {}
        self._issue_id = f"station_silent_{entry.entry_id}"
        self._title = entry.title
        self._issue_station: str | None = None  # the station the repair issue is raised for, if any

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_interval(self.hass, self._tick, timedelta(minutes=1)))
        for coordinator in self.runtime.coordinators:
            self.async_on_remove(coordinator.async_add_listener(self._refresh))
        self.async_on_remove(self._clear_issue)
        self._refresh()

    @callback
    def _tick(self, _now: datetime) -> None:
        self._refresh()

    @callback
    def _refresh(self) -> None:
        now = dt_util.utcnow()
        self._state = self._tracker.evaluate(now, feed_conditions(self.runtime, now))
        self._sync_issue(now)
        self.async_write_ha_state()

    @callback
    def _sync_issue(self, now: datetime) -> None:
        """A station silent past the grace is also a repair issue (HA quality scale: repair-issues): the user can fix
        it by entering another station id or by running Reconfigure. It goes when the station answers again."""
        station = None
        if self._state["amedas_station"]["problem"] and (silent := silent_station(self.runtime, now)) is not None:
            station = silent[0]
        if station == self._issue_station:
            return
        if station is None:
            self._clear_issue()
            return
        ir.async_create_issue(
            self.hass, DOMAIN, self._issue_id, is_fixable=False, severity=ir.IssueSeverity.WARNING,
            translation_key="station_silent",
            translation_placeholders={"station": station, "title": self._title})
        self._issue_station = station

    @callback
    def _clear_issue(self) -> None:
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
        self._issue_station = None

    @property
    def is_on(self) -> bool:
        return any(v["problem"] for v in self._state.values())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._state
