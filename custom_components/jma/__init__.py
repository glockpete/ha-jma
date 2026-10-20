"""JMA (気象庁) weather, warnings, rain nowcast and earthquakes for a place in Japan.

Data: JMA's /bosai/ JSON and tiles, JMA's XML feed (as a cross-check), Open-Meteo's JMA MSM
model for hourly, P2P地震情報 for quakes and EEW.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import voluptuous as vol
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
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
    TSUNAMI_HOME_AREA,
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
from .parse_quake import QuakePlace, prefecture_for_office
from .quake_ws import QuakeClient

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.BINARY_SENSOR, Platform.EVENT, Platform.SENSOR, Platform.WEATHER]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
SERVICE_SIMULATE = "simulate"
# The earthquake service allows two WebSocket connections per IP address; one is kept open per entry.
MAX_QUAKE_SOCKETS = 2
TSUNAMI_TICK = timedelta(minutes=10)
SIMULATE_SCHEMA = vol.Schema({
    vol.Required("event_type"): vol.In(EVENT_TYPES),
    vol.Optional("data", default={}): dict,
})


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
    # the area as stored by the config flow. Without a stored name the local values are unknown and nothing
    # else is affected.
    place = QuakePlace(prefecture_for_office(cfg.office), data.get(CONF_AREA_NAME_JA) or None)
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
                          tsunami_area=TSUNAMI_HOME_AREA.get(cfg.office)),
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
            runtime.extra["station_names"] = {
                sid: (table.get(sid) or {}).get("kjName")
                for sid in (cfg.station_primary, cfg.station_secondary)
            }
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

    await asyncio.gather(names_then_heat(), *(
        c.async_refresh() for c in runtime.coordinators if c is not runtime.heat_alert))
    # Decided before the entities exist, so the earthquake sensor starts with the right availability.
    sockets: set[str] = hass.data.setdefault(DOMAIN, {}).setdefault("quake_sockets", set())
    if len(sockets) < MAX_QUAKE_SOCKETS:
        sockets.add(entry.entry_id)
        entry.async_on_unload(lambda: sockets.discard(entry.entry_id))
    else:
        runtime.quake.enabled = False
        _LOGGER.warning(
            "No earthquake connection for %s: the earthquake service allows %d connections per IP address and "
            "%d are in use by other entries", entry.title, MAX_QUAKE_SOCKETS, MAX_QUAKE_SOCKETS)
    entry.runtime_data = runtime
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    runtime.set_alerts_ready()  # events held from the first polls (warning_in_force, ...) go out now
    if runtime.quake.enabled:
        entry.async_create_background_task(hass, runtime.quake.run(), "jma p2p quake socket")

    @callback
    def tsunami_tick(_now: datetime) -> None:
        """A tsunami bulletin that was never cancelled expires on the clock, not on the next message."""
        runtime.quake.tsunami_tick(dt_util.now())

    entry.async_on_unload(async_track_time_interval(hass, tsunami_tick, TSUNAMI_TICK))

    if runtime.debug_simulate:
        async def simulate(call: ServiceCall) -> None:
            """Test-only: fire an alert event as if JMA/P2P had produced it."""
            payload = {**call.data["data"], "simulated": True}
            async_dispatcher_send(hass, signal_alert(entry.entry_id), call.data["event_type"], payload)

        hass.services.async_register(DOMAIN, SERVICE_SIMULATE, simulate, schema=SIMULATE_SCHEMA)
        entry.async_on_unload(lambda: hass.services.async_remove(DOMAIN, SERVICE_SIMULATE))

    entry.async_on_unload(entry.add_update_listener(_reload))
    return True


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
    """The alert memory file goes with the entry."""
    await AlertMemory(hass, entry.entry_id).async_remove()
