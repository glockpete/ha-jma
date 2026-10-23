"""Polling coordinators and the shared runtime.

Failure policy: a failed poll keeps the last good data until it is older than STALE_FACTOR x
the interval; after that the coordinator fails and its entities go unavailable.
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from homeassistant.components.weather import Forecast
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import JmaApi
from .const import (
    EEW_FUTURE_TOLERANCE_S,
    IN_FORCE_AT_START_MIN_LEVEL,
    INTERVAL_AMEDAS,
    INTERVAL_FORECAST,
    INTERVAL_HEAT_ALERT,
    INTERVAL_HOURLY,
    INTERVAL_JAPAN,
    INTERVAL_LAYERS,
    INTERVAL_NOWCAST,
    INTERVAL_TIMELINE,
    INTERVAL_TYPHOON,
    INTERVAL_WARNINGS,
    INTERVAL_WBGT,
    INTERVAL_XML,
    KIKIKURU_ELEMENTS,
    LAYERS_ZOOM,
    NOWCAST_MAX_FORECAST,
    NOWCAST_PAST_FRAMES,
    NOWCAST_ZOOM,
    RASRF_MAX_FRAMES,
    STALE_FACTOR,
    TILE_MAX_INFLIGHT,
    TYPHOON_MAX_STORMS,
    URL_AMEDAS_LATEST,
    URL_AMEDAS_POINT,
    URL_AMEDAS_TABLE,
    URL_FORECAST,
    URL_HEAT_ALERT,
    URL_HIMAWARI_TIMES,
    URL_JAPAN_FORECAST,
    URL_NOWC_TILE,
    URL_NOWC_TIMES,
    URL_OPEN_METEO,
    URL_OVERVIEW,
    URL_RASRF_TILE,
    URL_RASRF_TIMES,
    URL_RISK_TILE,
    URL_RISK_TIMES,
    URL_TYPHOON_FORECAST,
    URL_TYPHOON_SPECS,
    URL_TYPHOON_TARGET,
    URL_WARNING_TIMELINE,
    URL_WARNINGS,
    URL_WBGT,
    URL_XML_EXTRA,
    WARNINGS_SUSPECT_MAX_POLLS,
    WBGT_PREF,
)
from .errors import JmaError, JmaFetchError, JmaParseError
from .memory import AlertMemory
from .parse_amedas import (
    Observation,
    merge_observations,
    parse_latest_time,
    parse_point,
    point_file_parts,
    station_names_from_table,
)
from .parse_forecast import ForecastData, parse_forecast, parse_overview
from .parse_heat_alert import (
    HEAT_ALERT_NON_ALERT_STATUSES,
    HeatAlertBulletin,
    expected_files,
    file_status,
    in_season,
    new_alerts,
    parse_heat_alert,
    with_home,
)
from .parse_hourly import open_meteo_params, parse_hourly
from .parse_japan import JapanForecast, parse_japan
from .parse_layers import (
    Kikikuru,
    LastGood,
    RainOutlook,
    himawari_frames,
    kikikuru_level_at,
    layers_attribute,
    liden_frames,
    n3_frames,
    rain_outlook,
    rasrf_frames,
    risk_frames,
)
from .parse_nowcast import (
    RainArming,
    RainSummary,
    WetHold,
    band_at,
    frame_lists,
    rain_series,
    tile_for,
)
from .parse_timeline import WarningOutlook, parse_timeline
from .parse_typhoon import ApproachTracker, approach_stage, fit_storms, parse_storm, parse_target_tc
from .parse_warnings import (
    AreaWarnings,
    WarningChange,
    diff_warnings,
    parse_area_warnings,
    stale_bulletins,
    suspect_reason,
)
from .parse_warnings import Warning as AreaWarning
from .parse_wbgt import WbgtForecast, parse_wbgt

if TYPE_CHECKING:
    from .quake_ws import QuakeClient

_LOGGER = logging.getLogger(__name__)
T = TypeVar("T")
# Raised alerts: (event type, payload) to the event entity.
type FireCallback = Callable[[str, dict[str, Any]], None]


def _first(items: list[T], limit: int, what: str) -> list[T]:
    """The first `limit` entries of a feed's list. The rest is ignored (one DEBUG line), so a feed that lists
    thousands cannot make one poll fan out to thousands of requests."""
    if len(items) > limit:
        _LOGGER.debug("%s: %d listed, only the first %d are read", what, len(items), limit)
        return items[:limit]
    return items


async def _limited(sem: asyncio.Semaphore, request: Callable[..., Awaitable[T]], *args: Any) -> T:
    """One request of a fan-out; the poll's semaphore keeps at most TILE_MAX_INFLIGHT of them in flight."""
    async with sem:
        return await request(*args)


def signal_alert(entry_id: str) -> str:
    return f"jma_alert_{entry_id}"


def signal_quake(entry_id: str) -> str:
    return f"jma_quake_{entry_id}"


@dataclass(frozen=True)
class JmaConfig:
    """What the config entry resolved to."""

    office: str
    area: str
    area_name: str
    class10: str
    station_primary: str
    station_secondary: str
    latitude: float
    longitude: float


class _JmaCoordinator(DataUpdateCoordinator[T], Generic[T]):
    """Keeps the last good data through short outages."""

    config_entry: JmaConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: JmaConfigEntry, api: JmaApi, cfg: JmaConfig,
        name: str, interval: timedelta,
    ) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=f"jma {name}",
                         update_interval=interval)
        self.api = api
        self.cfg = cfg
        self.last_ok: datetime | None = None
        self.last_error: str | None = None
        # The class of the last failure (None after a good poll): setup reads it to tell "no route to JMA"
        # (a fetch error) from a file that arrived but was not what the parser expects.
        self.last_error_type: type[BaseException] | None = None
        # First poll: feed health measures "silent since" from here when nothing ever succeeded.
        self.first_try: datetime | None = None
        # The run of failed polls since the last good one, for the log: one WARNING for a payload surprise, DEBUG
        # while the run goes on, one INFO when data is back. (HA's DataUpdateCoordinator already logs once, and
        # once on recovery, when UpdateFailed is raised; nothing is added for that path.)
        self._failed_polls = 0
        self._payload_warned = False

    async def _fetch(self) -> T:
        raise NotImplementedError

    async def _async_update_data(self) -> T:
        if self.first_try is None:
            self.first_try = dt_util.utcnow()
        try:
            data = await self._fetch()
        except (JmaError, KeyError, IndexError, TypeError, ValueError, OverflowError, csv.Error, AttributeError) as err:
            # Parser surprises (a ragged array, a bad time string, a field that changed its type, a number
            # too big for a float, a CSV field over the reader's limit) get the same keep-last-good
            # treatment as fetch errors instead of HA's immediate unavailable and a traceback in the log.
            self._failed_polls += 1
            if not isinstance(err, JmaError):
                # Once per run of failures, not once per poll: the feed repeats the same surprise every interval.
                log = _LOGGER.debug if self._payload_warned else _LOGGER.warning
                self._payload_warned = True
                log("%s: unexpected payload: %r", self.name, err)
            self.last_error = str(err)
            self.last_error_type = type(err)
            now = dt_util.utcnow()
            if (
                self.data is not None
                and self.last_ok is not None
                and self.update_interval is not None
                and now - self.last_ok < self.update_interval * STALE_FACTOR
            ):
                _LOGGER.debug("%s: keeping last good data: %s", self.name, err)
                return self.data
            raise UpdateFailed(str(err)) from err
        if self._payload_warned:
            _LOGGER.info("%s: data is back after %d failed poll(s)", self.name, self._failed_polls)
        self._failed_polls = 0
        self._payload_warned = False
        self.last_ok = dt_util.utcnow()
        self.last_error = None
        self.last_error_type = None
        return data


class AlertEmitter:
    """Events a coordinator produces before the event entity listens are held, then sent.

    The first poll runs during setup, before the platforms exist: anything it finds waits in
    `_pending` until set_alerts_ready() (called from async_setup_entry once they are set up).
    """

    _fire: FireCallback

    def _init_emitter(self, fire: FireCallback) -> None:
        self._fire = fire
        self._alerts_ready = False
        self._pending: list[tuple[str, dict[str, Any]]] = []

    def set_alerts_ready(self) -> None:
        self._alerts_ready = True
        pending, self._pending = self._pending, []
        for event_type, data in pending:
            self._fire(event_type, data)

    def _emit(self, event_type: str, data: dict[str, Any]) -> None:
        if self._alerts_ready:
            self._fire(event_type, data)
        else:
            self._pending.append((event_type, data))


# ---------- forecast ----------

@dataclass(frozen=True)
class ForecastBundle:
    forecast: ForecastData
    overview: str | None
    overview_time: datetime | None


class ForecastCoordinator(_JmaCoordinator[ForecastBundle]):
    def __init__(self, hass: HomeAssistant, entry: JmaConfigEntry, api: JmaApi, cfg: JmaConfig) -> None:
        super().__init__(hass, entry, api, cfg, "forecast", INTERVAL_FORECAST)

    async def _fetch(self) -> ForecastBundle:
        data = await self.api.get_json(URL_FORECAST.format(office=self.cfg.office))
        forecast = parse_forecast(data, self.cfg.class10, self.cfg.station_secondary)
        overview = overview_time = None
        try:
            overview, overview_time = parse_overview(
                await self.api.get_json(URL_OVERVIEW.format(office=self.cfg.office)))
        except JmaError as err:
            _LOGGER.debug("overview unavailable: %s", err)
        return ForecastBundle(forecast, overview, overview_time)


# ---------- hourly (Open-Meteo JMA MSM) ----------

class HourlyCoordinator(_JmaCoordinator[list[Forecast]]):
    def __init__(self, hass: HomeAssistant, entry: JmaConfigEntry, api: JmaApi, cfg: JmaConfig) -> None:
        super().__init__(hass, entry, api, cfg, "hourly", INTERVAL_HOURLY)

    async def _fetch(self) -> list[Forecast]:
        data = await self.api.get_json(
            URL_OPEN_METEO, open_meteo_params(self.cfg.latitude, self.cfg.longitude))
        return parse_hourly(data)


# ---------- AMeDAS ----------

class AmedasCoordinator(_JmaCoordinator[Observation]):
    def __init__(self, hass: HomeAssistant, entry: JmaConfigEntry, api: JmaApi, cfg: JmaConfig) -> None:
        super().__init__(hass, entry, api, cfg, "amedas", INTERVAL_AMEDAS)
        # Per station: the other one fills every value, so one station can be down for days
        # unseen; feed health reads these (reason `amedas_station`).
        self.station_ok: dict[str, datetime | None] = {}
        self.station_error: dict[str, str | None] = {}

    async def _station(self, station: str, ymd: str, hh: str) -> Observation | None:
        try:
            data = await self.api.get_json(
                URL_AMEDAS_POINT.format(station=station, ymd=ymd, hh=hh))
            obs = parse_point(data, station)
        except JmaError as err:
            _LOGGER.debug("amedas %s: %s", station, err)
            self.station_error[station] = str(err)
            return None
        if obs.observed_at is None:
            self.station_error[station] = "no usable value in the file"
        else:
            self.station_ok[station] = dt_util.utcnow()
            self.station_error[station] = None
        return obs

    async def _fetch(self) -> Observation:
        latest = parse_latest_time(await self.api.get_text(URL_AMEDAS_LATEST))
        ymd, hh = point_file_parts(latest)
        if self.cfg.station_secondary == self.cfg.station_primary:
            # The nearest station is also the type-A one: ask for it once; it has nothing to fill from.
            primary, secondary = await self._station(self.cfg.station_primary, ymd, hh), None
        else:
            primary, secondary = await asyncio.gather(
                self._station(self.cfg.station_primary, ymd, hh),
                self._station(self.cfg.station_secondary, ymd, hh),
            )
        if primary is None and secondary is None:
            raise JmaError("amedas: neither station answered")
        return merge_observations(primary, secondary)


# ---------- warnings ----------

@dataclass(frozen=True)
class WarningsState:
    area: AreaWarnings
    changes: list[WarningChange]
    stale: list[str]
    xml_ok: datetime | None


class WarningsCoordinator(AlertEmitter, _JmaCoordinator[WarningsState]):
    def __init__(self, hass: HomeAssistant, entry: JmaConfigEntry, api: JmaApi, cfg: JmaConfig,
                 fire: FireCallback) -> None:
        super().__init__(hass, entry, api, cfg, "warnings", INTERVAL_WARNINGS)
        self._init_emitter(fire)
        self._prev: list[AreaWarning] | None = None
        self._prev_control: dict[str, str] | None = None
        self._suspect_polls = 0
        self._stale: list[str] = []
        self._xml_at: datetime | None = None
        self.xml_ok: datetime | None = None
        self.xml_error: str | None = None

    async def _check_xml(self, area: AreaWarnings) -> None:
        now = dt_util.utcnow()
        if self._xml_at is not None and now - self._xml_at < INTERVAL_XML:
            return
        self._xml_at = now
        try:
            xml = await self.api.get_text(URL_XML_EXTRA)
            self._stale = stale_bulletins(xml, self.cfg.office, area.control_times)
            self.xml_ok = now
            self.xml_error = None
        except JmaError as err:
            self.xml_error = str(err)

    def _hold_if_suspect(self, area: AreaWarnings) -> None:
        """A partial or older file would be diffed into 'cleared' then 'issued' again (a second
        L4 alarm). Hold it (keep-last-good) for a few polls; believe it if it persists."""
        reason = suspect_reason(self._prev_control, area)
        if reason is None:
            self._suspect_polls = 0
            return
        if self._suspect_polls < WARNINGS_SUSPECT_MAX_POLLS:
            self._suspect_polls += 1
            raise JmaParseError(f"r8 held ({self._suspect_polls}/{WARNINGS_SUSPECT_MAX_POLLS}): {reason}")
        _LOGGER.warning("r8 warnings: accepting a file that still looks partial or older: %s", reason)
        self._suspect_polls = 0

    def _payload(self, change: WarningChange, area: AreaWarnings) -> dict[str, Any]:
        w = change.warning
        return {
            "phenomenon": change.phenomenon,
            "old_level": change.old_level,
            "new_level": change.new_level,
            "code": w.code,
            "name_ja": w.name_ja,
            "name_en": w.name_en,
            "status": w.status,
            "area": self.cfg.area_name,
            "headline": (area.headlines.get(w.data_type) if w.data_type is not None else None) or area.headline,
            "report_time": w.report_time,
        }

    async def _fetch(self) -> WarningsState:
        records = await self.api.get_json(URL_WARNINGS.format(office=self.cfg.office))
        area = parse_area_warnings(records, self.cfg.area)
        await self._check_xml(area)
        self._hold_if_suspect(area)
        if self._prev is None:
            # First poll after a (re)start: nothing is diffed, but an L4/L5 already in force is
            # announced once as `warning_in_force` (an automation decides how loud).
            changes = [
                WarningChange("warning_in_force", c.phenomenon, 0, c.new_level, c.warning)
                for c in diff_warnings([], area.warnings)
                if c.new_level >= IN_FORCE_AT_START_MIN_LEVEL
            ]
        else:
            changes = diff_warnings(self._prev, area.warnings)
        self._prev = area.warnings
        self._prev_control = dict(area.control_times)
        for change in changes:
            self._emit(change.event_type, self._payload(change, area))
        return WarningsState(area, changes, list(self._stale), self.xml_ok)


# ---------- nowcast ----------

@dataclass(frozen=True)
class NowcastState:
    basetime: str
    past: list[str]
    forecast: list[str]
    rain: RainSummary
    wet: bool = False


class NowcastCoordinator(_JmaCoordinator[NowcastState]):
    def __init__(self, hass: HomeAssistant, entry: JmaConfigEntry, api: JmaApi, cfg: JmaConfig,
                 fire: FireCallback) -> None:
        super().__init__(hass, entry, api, cfg, "nowcast", INTERVAL_NOWCAST)
        self._fire = fire
        self._tile = tile_for(cfg.latitude, cfg.longitude, NOWCAST_ZOOM)
        self._bands: dict[tuple[str, str], int | None] = {}
        self._arming = RainArming()
        self._wet_hold = WetHold()
        # Set once the event entity exists: an episode judged on the first poll (before the
        # platforms are set up) would be disarmed with nobody listening, and never pushed.
        self.alerts_ready = False

    async def _band(self, basetime: str, validtime: str, sem: asyncio.Semaphore) -> int | None:
        key = (basetime, validtime)
        if key not in self._bands:
            x, y, px, py = self._tile
            png = await _limited(sem, self.api.get_bytes, URL_NOWC_TILE.format(
                basetime=basetime, validtime=validtime, z=NOWCAST_ZOOM, x=x, y=y))
            if png is None:
                return None  # not on the CDN yet: ask again next poll, never cache it as dry
            # Pillow decodes the PNG: blocking work, kept off the event loop.
            self._bands[key] = await self.hass.async_add_executor_job(band_at, png, px, py)
        return self._bands[key]

    async def _fetch(self) -> NowcastState:
        n1, n2 = await asyncio.gather(
            self.api.get_json(URL_NOWC_TIMES.format(n="N1")),
            self.api.get_json(URL_NOWC_TIMES.format(n="N2")),
        )
        basetime, past, forecast = frame_lists(n1, n2, NOWCAST_PAST_FRAMES)
        forecast = _first(forecast, NOWCAST_MAX_FORECAST, "nowcast forecast frames")
        valid = [basetime, *forecast]
        sem = asyncio.Semaphore(TILE_MAX_INFLIGHT)
        bands = await asyncio.gather(*(self._band(basetime, v, sem) for v in valid))
        # Tiles for a basetime never change; keep only the current basetime's.
        self._bands = {k: v for k, v in self._bands.items() if k[0] == basetime}
        rain = rain_series(basetime, list(zip(valid, bands, strict=False)))
        wet = self._wet_hold.update(
            basetime, None if rain.series[0]["band"] is None else rain.raining_now)
        if self.alerts_ready and self._arming.update(rain, dt_util.utcnow()):
            first = next(s for s in rain.series[1:] if s["minutes"] == rain.minutes_to_rain)
            self._fire("rain_arriving", {
                "minutes": rain.minutes_to_rain,
                "band": first["band"],
                "mmh_upper": first["mmh_upper"],
                "time": first["time"],
                "max_mmh": rain.max_mmh,
            })
        return NowcastState(basetime, past, forecast, rain, wet)


# ---------- timeline, typhoon, heat index, map layers: optional, never part of feed health ----------

class WarningOutlookCoordinator(_JmaCoordinator[WarningOutlook]):
    """3-hourly warning-risk outlook for the area (危険度の時系列)."""

    def __init__(self, hass: HomeAssistant, entry: JmaConfigEntry, api: JmaApi, cfg: JmaConfig) -> None:
        super().__init__(hass, entry, api, cfg, "warning outlook", INTERVAL_TIMELINE)

    async def _fetch(self) -> WarningOutlook:
        data = await self.api.get_json(URL_WARNING_TIMELINE.format(office=self.cfg.office))
        return parse_timeline(data, self.cfg.area)


@dataclass(frozen=True)
class TyphoonState:
    count: int
    storms: list[dict[str, Any]]


class TyphoonCoordinator(AlertEmitter, _JmaCoordinator[TyphoonState]):
    """Active tropical cyclones. No storm = count 0, still a good poll.

    typhoon_approach / typhoon_cleared events; `memory` is the "typhoon"
    section of AlertMemory ({storm id: {"stage", "quiet", "last_issue", "absent", "number",
    "name_en", "name_ja"}}), `save` persists it.
    """

    def __init__(self, hass: HomeAssistant, entry: JmaConfigEntry, api: JmaApi, cfg: JmaConfig,
                 fire: FireCallback | None = None,
                 memory: dict[str, Any] | None = None, save: Callable[[], None] | None = None) -> None:
        super().__init__(hass, entry, api, cfg, "typhoon", INTERVAL_TYPHOON)
        self._init_emitter(fire or (lambda _type, _data: None))
        self._memory: dict[str, Any] = memory if memory is not None else {}
        self._save: Callable[[], None] = save or (lambda: None)
        self._storms: dict[str, tuple[dict[str, Any], datetime]] = {}
        self._tracker = ApproachTracker(self._memory)
        self._fresh: set[str] = set()  # storms parsed in the current poll (not kept from last-good)

    async def _storm(self, tc: dict[str, Any], now: datetime, sem: asyncio.Semaphore) -> dict[str, Any]:
        """One storm; a failed fetch keeps that storm's last good entry while it is fresh."""
        try:
            forecast, specs = await asyncio.gather(
                _limited(sem, self.api.get_json, URL_TYPHOON_FORECAST.format(tc=tc["id"])),
                _limited(sem, self.api.get_json, URL_TYPHOON_SPECS.format(tc=tc["id"])),
            )
            storm = parse_storm(tc, forecast, specs, (self.cfg.latitude, self.cfg.longitude))
        except (JmaError, KeyError, IndexError, TypeError, ValueError, OverflowError, AttributeError) as err:
            # One storm's surprise (a bad shape, a failed file) must not mute the others:
            # keep its last good entry while fresh, else list it bare.
            prev = self._storms.get(tc["id"])
            if prev is not None and now - prev[1] < INTERVAL_TYPHOON * STALE_FACTOR:
                _LOGGER.debug("typhoon %s: keeping last good: %s", tc["id"], err, exc_info=True)
                return prev[0]
            if isinstance(err, JmaError):
                _LOGGER.debug("typhoon %s: listed without details: %s", tc["id"], err, exc_info=True)
            else:
                _LOGGER.warning("typhoon %s: unexpected payload, listed without details: %r",
                                tc["id"], err, exc_info=True)
            return {
                "id": tc["id"], "number": tc.get("number"), "category": tc.get("category"),
                "issue": tc.get("issue"), "now": None, "track": [], "past_track": [],
                "distance_km": None, "closest": None, "incomplete": True,
            }
        self._storms[tc["id"]] = (storm, now)
        self._fresh.add(tc["id"])
        return storm

    def _approach(self, storms: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
        """Set each complete storm's `stage`, tell the tracker (only about storms parsed in THIS
        poll: a storm kept from last-good goes in with stage None and is not a quiet poll), send
        the events and save the memory when it changed. Returns the storms with their stage."""
        staged = [s if s.get("incomplete") else {**s, "stage": approach_stage(s, now)} for s in storms]
        for event_type, data in self._tracker.update(
                [s if s["id"] in self._fresh else {**s, "stage": None} for s in staged], now):
            self._emit(event_type, data)
        if self._tracker.dirty:
            self._tracker.dirty = False
            self._save()
        return staged

    async def _fetch(self) -> TyphoonState:
        now = dt_util.utcnow()
        body = await self.api.get_bytes(URL_TYPHOON_TARGET)
        if body is None:
            # No storm known yet: if JMA drops the file (404) when no storm is active, that is "none".
            self._storms = {}
            self._fresh = set()
            self._approach([], now)  # a storm that was in memory has gone from the list
            return TyphoonState(0, [])
        try:
            listed = json.loads(body)
        except ValueError as err:
            raise JmaParseError(f"typhoon targetTc: not JSON ({body[:60]!r})") from err
        tcs = _first(parse_target_tc(listed), TYPHOON_MAX_STORMS, "typhoon storms")
        self._fresh = set()
        sem = asyncio.Semaphore(TILE_MAX_INFLIGHT)
        storms = list(await asyncio.gather(*(self._storm(tc, now, sem) for tc in tcs)))
        live = {tc["id"] for tc in tcs}
        self._storms = {k: v for k, v in self._storms.items() if k in live}
        return TyphoonState(len(tcs), fit_storms(self._approach(storms, now)))


@dataclass(frozen=True)
class WbgtState:
    forecast: WbgtForecast | None  # None: out of season, 404, empty, or the office is not mapped


_JST = timezone(timedelta(hours=9))


class WbgtCoordinator(_JmaCoordinator[WbgtState]):
    """環境省 WBGT forecast. "No data" is a quiet, successful poll (debug log only)."""

    def __init__(self, hass: HomeAssistant, entry: JmaConfigEntry, api: JmaApi, cfg: JmaConfig) -> None:
        super().__init__(hass, entry, api, cfg, "wbgt", INTERVAL_WBGT)

    async def _fetch(self) -> WbgtState:
        pref = WBGT_PREF.get(self.cfg.office)
        if pref is None:
            _LOGGER.debug("wbgt: no CSV mapped for office %s", self.cfg.office)
            return WbgtState(None)
        body = await self.api.get_bytes(URL_WBGT.format(pref=pref))
        if body is None:
            _LOGGER.debug("wbgt: CSV not published (404)")
            return WbgtState(None)
        today = dt_util.utcnow().astimezone(_JST).date()
        forecast = parse_wbgt(body.decode("utf-8", errors="replace"),
                              (self.cfg.station_primary, self.cfg.station_secondary), today)
        if forecast is None:
            _LOGGER.debug("wbgt: no usable row for %s / %s",
                          self.cfg.station_primary, self.cfg.station_secondary)
        return WbgtState(forecast)


@dataclass(frozen=True)
class HeatAlertState:
    """bulletin: the newest parsed file (parse_heat_alert.HeatAlertBulletin) or None: out of
    season, not published yet, a drill/test file, or the office has no row."""

    bulletin: HeatAlertBulletin | None


class HeatAlertCoordinator(AlertEmitter, _JmaCoordinator[HeatAlertState]):
    """環境省 熱中症警戒アラート / 特別警戒アラート for the office's prefecture row.

    Optional data: "no file" is a quiet, successful poll and it is not part of feed health.
    `memory` is the "heat" list of AlertMemory (keys already announced), `save` persists it.
    """

    MAX_REQUESTS = 2  # files requested per poll (the newest, then one fallback)

    def __init__(self, hass: HomeAssistant, entry: JmaConfigEntry, api: JmaApi, cfg: JmaConfig,
                 fire: FireCallback | None = None,
                 memory: list[str] | None = None, save: Callable[[], None] | None = None) -> None:
        super().__init__(hass, entry, api, cfg, "heat alert", INTERVAL_HEAT_ALERT)
        self._init_emitter(fire or (lambda _type, _data: None))
        self._memory: list[str] = memory if memory is not None else []
        self._save: Callable[[], None] = save or (lambda: None)
        self._held: tuple[tuple[date, int], HeatAlertBulletin] | None = None  # (issue date, hour), bulletin
        self._missing: dict[tuple[date, int], datetime] = {}  # name -> when it answered 404 / unusable
        self._warned: set[tuple[date, int]] = set()  # unusable files already logged at WARNING
        # AMeDAS station NAMES (primary, secondary) as the CSV writes them. __init__ sets it after
        # the first table fetch; while it is empty _fetch asks amedastable.json again (R2-7).
        self.station_names: tuple[str, ...] = ()

    async def _load_station_names(self) -> None:
        """Names are optional: a failure or an odd shape is a debug line and the poll goes on."""
        try:
            table = await self.api.get_json(URL_AMEDAS_TABLE)
            names = tuple(name for name in station_names_from_table(
                table, (self.cfg.station_primary, self.cfg.station_secondary)) if name)
        except (JmaError, AttributeError, TypeError) as err:
            _LOGGER.debug("heat alert: no station names yet: %s", err)
            return
        self.station_names = names
        if names and self._held is not None:  # the held file is re-read, not re-requested
            self._held = (self._held[0], with_home(self._held[1], names))

    async def _fetch(self) -> HeatAlertState:
        now = dt_util.utcnow().astimezone(_JST)
        today = now.date()
        if not in_season(today):
            self._held, self._missing = None, {}
            return HeatAlertState(None)  # out of season: no request at all
        if not self.station_names:
            await self._load_station_names()
        held_name = self._held[0] if self._held else None
        found: tuple[tuple[date, int], HeatAlertBulletin] | None = None
        requests = 0
        for i, name in enumerate(expected_files(now)):
            if name == held_name:
                break  # already have this one (or a newer one): nothing to request
            if requests >= self.MAX_REQUESTS:
                break
            missed = self._missing.get(name)
            if i > 0 and missed is not None and now - missed < timedelta(hours=1):
                continue  # answered 404 / unusable within the last hour; only the newest is retried
            requests += 1
            day, hour = name
            body = await self.api.get_bytes(URL_HEAT_ALERT.format(
                year=day.year, ymd=day.strftime("%Y%m%d"), hh=f"{hour:02d}"))
            text = None if body is None else body.decode("utf-8", errors="replace")
            bulletin = None if text is None else parse_heat_alert(
                text, self.cfg.office, self.station_names)
            if bulletin is not None and bulletin.report_time - now > timedelta(seconds=EEW_FUTURE_TOLERANCE_S):
                # Dated ahead of the clock (the same tolerance as the EEW and tsunami guards): not believed.
                # Held as the newest it would make every real file after it "older", for good.
                self._warn_future(name, bulletin)
                self._missing[name] = now
                continue
            if bulletin is None:
                self._missing[name] = now  # 404, a drill / test file, or no row for the office
                if text is not None:
                    self._log_unusable(name, text)
                continue
            found = (name, bulletin)
            break
        self._missing = {k: v for k, v in self._missing.items() if now - v < timedelta(hours=1)}
        if found is not None and (self._held is None or found[1].report_time > self._held[1].report_time):
            self._held = found
            events = new_alerts(found[1], today, self._memory)
            for data in events:
                self._emit("heat_alert", data)
            if events:
                self._save()
        return HeatAlertState(self._held[1] if self._held else None)

    def _warn_future(self, name: tuple[date, int], bulletin: HeatAlertBulletin) -> None:
        """Once per file name, like _log_unusable."""
        if name not in self._warned:
            self._warned.add(name)
            _LOGGER.warning("heat alert: alert_%s_%02d.csv is dated %s, ahead of the clock: ignored",
                            f"{name[0]:%Y%m%d}", name[1], bulletin.report_time.isoformat())

    def _log_unusable(self, name: tuple[date, int], text: str) -> None:
        """A 200 answer with no bulletin: a drill / test file is expected (debug); anything else may
        be a changed layout, so say so once per file name."""
        label = f"alert_{name[0]:%Y%m%d}_{name[1]:02d}.csv"
        if file_status(text) in HEAT_ALERT_NON_ALERT_STATUSES:
            _LOGGER.debug("heat alert: %s is a drill / test file", label)
        elif name not in self._warned:
            self._warned.add(name)
            _LOGGER.warning("heat alert: %s answered 200 but has no usable row for office %s "
                            "(status %r); a changed file layout would look like this",
                            label, self.cfg.office, file_status(text))


class JapanCoordinator(_JmaCoordinator[JapanForecast]):
    """Nationwide city forecast, today + tomorrow. Optional: not in feed health."""

    def __init__(self, hass: HomeAssistant, entry: JmaConfigEntry, api: JmaApi, cfg: JmaConfig) -> None:
        super().__init__(hass, entry, api, cfg, "japan", INTERVAL_JAPAN)

    async def _fetch(self) -> JapanForecast:
        data = await self.api.get_json(URL_JAPAN_FORECAST)
        return parse_japan(data, dt_util.utcnow().astimezone(_JST).date())


@dataclass(frozen=True)
class LayersState:
    layers: dict[str, dict[str, Any]]
    kikikuru: Kikikuru | None
    rain_outlook: RainOutlook | None


def _n3_layers(data: Any) -> dict[str, list[dict[str, str]]]:
    # liden rides on the same N3 file: no extra request.
    return {"thns": n3_frames(data, "thns"), "trns": n3_frames(data, "trns"),
            "liden": liden_frames(data)}


class LayersCoordinator(_JmaCoordinator[LayersState]):
    """Frame lists for the map layers + キキクル and rasrf read at the configured place.

    Four lists are fetched together; each keeps its own last good value (LastGood), so one
    failing list never drops the others. Only when none is usable does the poll fail.
    """

    SOURCES: dict[str, tuple[str, Callable[[Any], Any]]] = {
        "n3": (URL_NOWC_TIMES.format(n="N3"), _n3_layers),
        "risk": (URL_RISK_TIMES, risk_frames),
        "rasrf": (URL_RASRF_TIMES, rasrf_frames),
        "himawari": (URL_HIMAWARI_TIMES, himawari_frames),
    }

    def __init__(self, hass: HomeAssistant, entry: JmaConfigEntry, api: JmaApi, cfg: JmaConfig) -> None:
        super().__init__(hass, entry, api, cfg, "layers", INTERVAL_LAYERS)
        max_age = INTERVAL_LAYERS * STALE_FACTOR
        # The four sources hold different shapes of frame list (see SOURCES), so the value type is open.
        self.sources: dict[str, LastGood[Any]] = {k: LastGood(max_age) for k in self.SOURCES}
        self._kikikuru: LastGood[Kikikuru] = LastGood(max_age)
        self._rain: LastGood[RainOutlook] = LastGood(max_age)
        self._tile = tile_for(cfg.latitude, cfg.longitude, LAYERS_ZOOM)
        # Decoded readings per tile; a tile for a given frame never changes.
        self._risk_levels: dict[tuple[str, str, str, str], int] = {}
        self._rasrf_bands: dict[tuple[str, str, str], int] = {}

    async def _source(self, key: str, now: datetime) -> None:
        url, parse = self.SOURCES[key]
        try:
            self.sources[key].ok(parse(await self.api.get_json(url)), now)
        except (JmaError, KeyError, IndexError, TypeError, ValueError) as err:
            self.sources[key].fail(err)
            _LOGGER.debug("layers %s: %s", key, err)

    async def _risk_level(self, frame: dict[str, str], elem: str) -> int | None:
        key = (frame["basetime"], frame["member"], frame["validtime"], elem)
        if key not in self._risk_levels:
            x, y, px, py = self._tile
            png = await self.api.get_bytes(URL_RISK_TILE.format(
                basetime=frame["basetime"], member=frame["member"], validtime=frame["validtime"],
                elem=elem, z=LAYERS_ZOOM, x=x, y=y))
            if png is None:
                return None  # not on the CDN yet: ask again next poll, never cache it as level 0
            self._risk_levels[key] = await self.hass.async_add_executor_job(
                kikikuru_level_at, png, px, py, elem)
        return self._risk_levels[key]

    async def _read_kikikuru(self, risk: list[dict[str, str]] | None, listed_at: datetime | None) -> None:
        """A reading is as old as the list it came from (listed_at), so it expires with it."""
        if not risk or listed_at is None:
            return
        frame = risk[-1]
        try:
            levels = await asyncio.gather(
                *(self._risk_level(frame, e) for e in KIKIKURU_ELEMENTS.values()))
        except JmaError as err:
            self._kikikuru.fail(err)
            _LOGGER.debug("kikikuru: %s", err)
            return
        current = (frame["basetime"], frame["member"], frame["validtime"])
        self._risk_levels = {k: v for k, v in self._risk_levels.items() if k[:3] == current}
        reading = Kikikuru(frame["basetime"], **dict(zip(KIKIKURU_ELEMENTS, levels, strict=False)))
        if reading.level is None:
            self._kikikuru.fail("no キキクル tile for the newest frame yet")
        else:
            self._kikikuru.ok(reading, listed_at)

    async def _rasrf_band(self, frame: dict[str, str], sem: asyncio.Semaphore) -> int | None:
        key = (frame["basetime"], frame["member"], frame["validtime"])
        if key not in self._rasrf_bands:
            x, y, px, py = self._tile
            png = await _limited(sem, self.api.get_bytes, URL_RASRF_TILE.format(
                basetime=frame["basetime"], member=frame["member"], validtime=frame["validtime"],
                z=LAYERS_ZOOM, x=x, y=y))
            if png is None:
                return None  # never cache a 404 as dry
            self._rasrf_bands[key] = await self.hass.async_add_executor_job(band_at, png, px, py)
        return self._rasrf_bands[key]

    async def _read_rain(self, rasrf: tuple[str, list[dict[str, str]]] | None,
                         listed_at: datetime | None) -> None:
        if not rasrf or listed_at is None:
            return
        basetime, frames = rasrf
        frames = _first(frames, RASRF_MAX_FRAMES, "rasrf frames")
        sem = asyncio.Semaphore(TILE_MAX_INFLIGHT)
        try:
            bands = await asyncio.gather(*(self._rasrf_band(f, sem) for f in frames))
        except JmaError as err:
            self._rain.fail(err)
            _LOGGER.debug("rain outlook: %s", err)
            return
        keep = {(f["basetime"], f["member"], f["validtime"]) for f in frames}
        self._rasrf_bands = {k: v for k, v in self._rasrf_bands.items() if k in keep}
        if frames and all(b is None for b in bands):
            self._rain.fail("no rasrf tile readable yet")
            return
        self._rain.ok(
            rain_outlook(basetime, [(f["validtime"], b) for f, b in zip(frames, bands, strict=False)]), listed_at)

    async def _fetch(self) -> LayersState:
        now = dt_util.utcnow()
        await asyncio.gather(*(self._source(k, now) for k in self.SOURCES))
        got = {k: s.get(now) for k, s in self.sources.items()}
        if all(v is None for v in got.values()):
            raise JmaFetchError("layers: no frame list usable: " + "; ".join(
                f"{k}: {s.last_error}" for k, s in self.sources.items()))
        await asyncio.gather(
            self._read_kikikuru(got["risk"], self.sources["risk"].last_ok),
            self._read_rain(got["rasrf"], self.sources["rasrf"].last_ok),
        )
        return LayersState(layers_attribute(**got), self._kikikuru.get(now), self._rain.get(now))


@dataclass
class JmaRuntime:
    """Everything one config entry owns."""

    cfg: JmaConfig
    api: JmaApi
    forecast: ForecastCoordinator
    hourly: HourlyCoordinator
    amedas: AmedasCoordinator
    warnings: WarningsCoordinator
    nowcast: NowcastCoordinator
    quake: QuakeClient
    timeline: WarningOutlookCoordinator
    typhoon: TyphoonCoordinator
    wbgt: WbgtCoordinator
    layers: LayersCoordinator
    japan: JapanCoordinator
    heat_alert: HeatAlertCoordinator
    memory: AlertMemory
    debug_simulate: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def coordinators(self) -> list[_JmaCoordinator[Any]]:
        return [
            self.forecast, self.hourly, self.amedas, self.warnings, self.nowcast,
            self.timeline, self.typhoon, self.wbgt, self.layers, self.japan, self.heat_alert,
        ]

    @property
    def unreachable(self) -> bool:
        """True when every core feed (forecast, warnings, AMeDAS, nowcast) failed its latest poll with a fetch error:
        no route to JMA. One feed that answered, a file that arrived but did not parse, or an optional feed that failed
        does not count."""
        core = (self.forecast, self.warnings, self.amedas, self.nowcast)
        return all(
            not c.last_update_success and c.last_error_type is not None and issubclass(c.last_error_type, JmaFetchError)
            for c in core)

    def set_alerts_ready(self) -> None:
        """Called once the platforms are set up: events held from the first polls go out now."""
        self.nowcast.alerts_ready = True
        for emitter in (self.warnings, self.typhoon, self.heat_alert):
            emitter.set_alerts_ready()


type JmaConfigEntry = ConfigEntry[JmaRuntime]


def make_fire(hass: HomeAssistant, entry_id: str) -> FireCallback:
    """Alerts go to the event entity through the dispatcher."""

    def fire(event_type: str, data: dict[str, Any]) -> None:
        async_dispatcher_send(hass, signal_alert(entry_id), event_type, data)

    return fire
