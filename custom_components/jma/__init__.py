"""JMA (気象庁) weather, warnings, rain nowcast and earthquakes for a place in Japan.

Data: JMA's /bosai/ JSON and tiles, JMA's XML feed (as a cross-check), Open-Meteo's JMA MSM
model for hourly, P2P地震情報 for quakes and EEW.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_CORE_CONFIG_UPDATE, Platform
from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .api import JmaApi
from .const import (
    CONF_AREA,
    CONF_AREA_NAME_JA,
    CONF_DEBUG_SIMULATE,
    CONF_OFFICE,
    CONF_STATION_PRIMARY,
    CONF_STATION_SECONDARY,
    DOMAIN,
    EVENT_TYPES,
    TSUNAMI_COAST_AREA,
    URL_AMEDAS_TABLE,
)
from .coordinator import (
    AmedasCoordinator,
    ForecastCoordinator,
    HeatAlertCoordinator,
    HourlyCoordinator,
    JapanCoordinator,
    JmaConfig,
    JmaConfigEntry,
    JmaRuntime,
    LayersCoordinator,
    NowcastCoordinator,
    TyphoonCoordinator,
    WarningOutlookCoordinator,
    WarningsCoordinator,
    WbgtCoordinator,
    make_fire,
    signal_alert,
    signal_quake,
)
from .errors import JmaError
from .memory import AlertMemory
from .parse_amedas import station_names_from_table
from .parse_quake import QuakePlace, prefecture_for_office
from .quake_ws import QuakeClient

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.BINARY_SENSOR, Platform.EVENT, Platform.SENSOR, Platform.WEATHER]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
SERVICE_SIMULATE = "simulate"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"
# The earthquake service allows two WebSocket connections per IP address; one is kept open per entry.
MAX_QUAKE_SOCKETS = 2
TSUNAMI_TICK = timedelta(minutes=10)
SIMULATE_SCHEMA = vol.Schema({
    vol.Required("event_type"): vol.In(EVENT_TYPES),
    vol.Optional("data", default={}): dict,
    vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
})


def _legacy_name(data: Mapping[str, Any]) -> str | None:
    """The Japanese municipality name of an entry made before 0.6: it was stored only as `area_name`. That key
    holds the numeric area code instead when no name was stored (the setup reads it with the code as default), so
    only a non-empty text that is not the entry's area code counts as a name."""
    name = data.get("area_name")
    if isinstance(name, str) and name and name != data.get(CONF_AREA):
        return name
    return None


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """The action is registered once, here (HA quality scale: action-setup), not by each entry: one entry's unload
    cannot take it away from another."""
    hass.services.async_register(DOMAIN, SERVICE_SIMULATE, _simulate, schema=SIMULATE_SCHEMA)
    return True


async def _simulate(call: ServiceCall) -> None:
    """Test-only: fire an alert event as if JMA/P2P had produced it, at the entry named by `config_entry_id` or, when
    none is named, at every loaded entry whose debug option is on."""
    hass = call.hass
    if (entry_id := call.data.get(ATTR_CONFIG_ENTRY_ID)) is not None:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="simulate_entry_not_found")
        if not entry.runtime_data.debug_simulate:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="simulate_not_enabled")
        targets = [entry]
    else:
        targets = [e for e in hass.config_entries.async_loaded_entries(DOMAIN) if e.runtime_data.debug_simulate]
        if not targets:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="simulate_no_debug_entry")
    for target in targets:
        payload = {**call.data["data"], "simulated": True}
        async_dispatcher_send(hass, signal_alert(target.entry_id), call.data["event_type"], payload)


async def async_setup_entry(hass: HomeAssistant, entry: JmaConfigEntry) -> bool:
    data = {**entry.data, **entry.options}
    cfg = JmaConfig(
        office=data[CONF_OFFICE],
        area=data[CONF_AREA],
        area_name=data.get("area_name", data[CONF_AREA]),
        class10=data["class10"],
        station_primary=data[CONF_STATION_PRIMARY],
        station_secondary=data[CONF_STATION_SECONDARY],
        latitude=hass.config.latitude,
        longitude=hass.config.longitude,
    )
    api = JmaApi(async_get_clientsession(hass))
    fire = make_fire(hass, entry.entry_id)
    # The quake part: the prefecture comes from the office code, the local name is the Japanese name of
    # the area as stored by the config flow (before 0.6: under `area_name`). Without a stored name the local
    # values are unknown and nothing else is affected. An entry in one of the 23 wards of Tokyo also gets the
    # area 東京都２３区, chosen by its area code.
    local_name = data.get(CONF_AREA_NAME_JA) or _legacy_name(data)
    place = QuakePlace.for_entry(prefecture_for_office(cfg.office), local_name, cfg.area)
    # What was already announced about typhoons, heat alerts and tsunami survives a restart.
    memory = AlertMemory(hass, entry.entry_id)
    await memory.async_load()
    # Alerts decided while Home Assistant is still starting are only remembered once it runs.
    entry.async_on_unload(memory.async_listen_started())

    @callback
    def on_quake() -> None:
        async_dispatcher_send(hass, signal_quake(entry.entry_id))

    runtime = JmaRuntime(
        cfg=cfg,
        api=api,
        forecast=ForecastCoordinator(hass, entry, api, cfg),
        hourly=HourlyCoordinator(hass, entry, api, cfg),
        amedas=AmedasCoordinator(hass, entry, api, cfg),
        warnings=WarningsCoordinator(hass, entry, api, cfg, fire),
        nowcast=NowcastCoordinator(hass, entry, api, cfg, fire),
        quake=QuakeClient(api, fire, on_quake, place, memory=memory.tsunami, save=memory.save,
                          tsunami_area=TSUNAMI_COAST_AREA.get(cfg.class10)),
        # Optional data: no events, no pushes, not part of feed health. The typhoon and heat-alert
        # coordinators also raise events.
        timeline=WarningOutlookCoordinator(hass, entry, api, cfg),
        typhoon=TyphoonCoordinator(hass, entry, api, cfg, fire, memory.typhoon, memory.save),
        wbgt=WbgtCoordinator(hass, entry, api, cfg),
        layers=LayersCoordinator(hass, entry, api, cfg),
        japan=JapanCoordinator(hass, entry, api, cfg),
        heat_alert=HeatAlertCoordinator(hass, entry, api, cfg, fire, memory.heat, memory.save),
        memory=memory,
        debug_simulate=bool(data.get(CONF_DEBUG_SIMULATE)),
    )
    async def station_names() -> None:
        """Station names for the weather attributes; one table fetch per start, names are optional."""
        try:
            table = await api.get_json(URL_AMEDAS_TABLE)
            ids = (cfg.station_primary, cfg.station_secondary)
            runtime.extra["station_names"] = dict(zip(ids, station_names_from_table(table, ids), strict=True))
        except (JmaError, AttributeError) as err:
            _LOGGER.debug("amedastable unavailable, no station names: %s", err)

    # What the update listener compares against: a change of title or settings reloads the entry.
    runtime.extra["settings"] = _settings(entry)

    # Never block setup on JMA: entities show unavailable until their feed answers.
    async def names_then_heat() -> None:
        """The heat alert coordinator reads its own station's figure out of the prefecture row by
        NAME (the CSV has no station ids), so its first poll waits for the names; nothing else
        does. If the table fails here the coordinator asks for it again on a later poll."""
        await station_names()
        runtime.heat_alert.station_names = tuple(
            name for name in (runtime.extra.get("station_names") or {}).values()
            if isinstance(name, str) and name)
        await runtime.heat_alert.async_refresh()

    # Two phases. The four core feeds first, because "no route to JMA" is decided from them alone: the heat-alert and
    # typhoon files come from other hosts and can answer while JMA does not, and their first poll DECIDES an alert,
    # saves it to the memory (a delayed write nothing cancels) and queues the event; raising afterwards throws the queue
    # away, and after the retry the memory would say "announced" for an event nobody saw.
    core = (runtime.forecast, runtime.warnings, runtime.amedas, runtime.nowcast)
    await asyncio.gather(*(c.async_refresh() for c in core))
    # HA quality scale (test-before-setup): with no route to JMA at all (all four core feeds failed to fetch) the entry
    # is not set up; Home Assistant retries with back-off and shows "Retrying setup". Anything else (one feed up, a
    # file that does not parse, an optional feed down) keeps setting up: entities wait for their feed. What was
    # registered above (the memory's started-listener) is undone by Home Assistant when this raises
    # (ConfigEntry.async_setup runs the on-unload callbacks of an entry that did not set up); the earthquake socket,
    # the platforms and the timers come after this point.
    if runtime.unreachable:
        raise ConfigEntryNotReady(translation_domain=DOMAIN, translation_key="jma_unreachable")
    await asyncio.gather(names_then_heat(), *(
        c.async_refresh() for c in runtime.coordinators if c is not runtime.heat_alert and c not in core))
    # Decided before the entities exist, so the earthquake sensor starts with the right availability.
    sockets: set[str] = hass.data.setdefault(DOMAIN, {}).setdefault("quake_sockets", set())
    issue_id = f"no_quake_socket_{entry.entry_id}"
    if len(sockets) < MAX_QUAKE_SOCKETS:
        sockets.add(entry.entry_id)

        @callback
        def release_socket() -> None:
            sockets.discard(entry.entry_id)
            if entry.disabled_by is not None:  # a disabled entry does not set up again: its connection goes on
                _offer_free_socket(hass)

        entry.async_on_unload(release_socket)
        ir.async_delete_issue(hass, DOMAIN, issue_id)  # a connection came free since the last setup
    else:
        runtime.quake.enabled = False
        _LOGGER.warning(
            "No earthquake connection for %s: the earthquake service allows %d connections per IP address and "
            "%d are in use by other entries", entry.title, MAX_QUAKE_SOCKETS, MAX_QUAKE_SOCKETS)
        # The log line is easy to miss: the repairs page tells the user why this entry has no earthquake data. It
        # goes when the entry unloads (a reload asks again, a freed connection then lets it set up with one).
        ir.async_create_issue(
            hass, DOMAIN, issue_id, is_fixable=False, severity=ir.IssueSeverity.WARNING,
            translation_key="no_quake_socket",
            translation_placeholders={"title": entry.title, "limit": str(MAX_QUAKE_SOCKETS)})
        entry.async_on_unload(lambda: ir.async_delete_issue(hass, DOMAIN, issue_id))
    entry.runtime_data = runtime
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    runtime.set_alerts_ready()  # events held from the first polls (warning_in_force, ...) go out now
    if runtime.quake.enabled:
        entry.async_create_background_task(hass, runtime.quake.run(), "jma p2p quake socket")

        @callback
        def tsunami_tick(_now: datetime) -> None:
            """A tsunami bulletin that was never cancelled expires on the clock, not on the next message."""
            runtime.quake.tsunami_tick(dt_util.now())

        # An entry without an earthquake connection has no tsunami bulletins to expire: no timer.
        entry.async_on_unload(async_track_time_interval(hass, tsunami_tick, TSUNAMI_TICK))

    @callback
    def core_config_updated(event: Event) -> None:
        """The user moved the home in Settings: the rain-nowcast tile is read for the coordinates copied into `cfg` at
        setup, so the entry sets up again to read the new ones. The event data is the dict of keys that were changed
        (Config.async_update), and the core has already stored the new values; any other change (unit system, time
        zone, name, elevation) is not ours."""
        changed = event.data or {}
        if "latitude" not in changed and "longitude" not in changed:
            return
        if (hass.config.latitude, hass.config.longitude) == (cfg.latitude, cfg.longitude):
            return  # saved again with the place it already had
        _LOGGER.debug("Reloading %s: Home Assistant's home moved", entry.title)
        hass.config_entries.async_schedule_reload(entry.entry_id)

    entry.async_on_unload(hass.bus.async_listen(EVENT_CORE_CONFIG_UPDATE, core_config_updated))
    entry.async_on_unload(entry.add_update_listener(_reload))
    return True


@callback
def _offer_free_socket(hass: HomeAssistant) -> None:
    """A removed or disabled entry gives its earthquake connection back. An entry that was set up while both were taken
    has none until its next setup, so as many of those as there are free connections are reloaded to take them. A
    plain reload of an entry does not come here: it takes its own connection again when it sets up."""
    sockets: set[str] = hass.data.get(DOMAIN, {}).get("quake_sockets", set())
    free = MAX_QUAKE_SOCKETS - len(sockets)
    if free <= 0:
        return
    waiting = [e for e in hass.config_entries.async_loaded_entries(DOMAIN) if e.entry_id not in sockets]
    for other in waiting[:free]:
        _LOGGER.debug("Reloading %s: an earthquake connection is free", other.title)
        hass.config_entries.async_schedule_reload(other.entry_id)


def _settings(entry: JmaConfigEntry) -> dict:
    """The entry's settings and its title (which the device carries)."""
    return {"_title": entry.title, **entry.data, **entry.options}


async def _reload(hass: HomeAssistant, entry: JmaConfigEntry) -> None:
    if entry.runtime_data.extra.get("settings") == _settings(entry):
        return  # nothing that matters changed
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: JmaConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        # a reload inside the save delay must not forget an alert it has just sent
        await entry.runtime_data.memory.async_flush()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: JmaConfigEntry) -> None:
    """The alert memory file goes with the entry, and its earthquake connection to an entry that has none."""
    _offer_free_socket(hass)
    await AlertMemory(hass, entry.entry_id).async_remove()
