"""Config flow: forecast office -> warnings area (class20) -> confirm the auto-picked stations.

JMA's class20 boundary GeoJSON is chunked by bounding box (class20relm.json), so the ward is
picked from a list rather than located from HA's home coordinates. AMeDAS stations are
chosen by distance: primary = nearest measuring temperature, secondary = nearest type A.
"""

from __future__ import annotations

import logging
import math
import re
import unicodedata
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import JmaApi
from .const import (
    CONF_AREA,
    CONF_AREA_NAME_JA,
    CONF_DEBUG_SIMULATE,
    CONF_OFFICE,
    CONF_STATION_PRIMARY,
    CONF_STATION_SECONDARY,
    DOMAIN,
    URL_AMEDAS_TABLE,
    URL_AREA,
)
from .errors import JmaError, JmaParseError

_LOGGER = logging.getLogger(__name__)
# What a file of the wrong shape raises on its way through the code that reads it: JSON that is valid but not what
# JMA publishes (a list for an object, a missing key, a row without coordinates) must end the flow, not crash it.
_SHAPE_ERRORS = (JmaError, LookupError, TypeError, AttributeError, ValueError)


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p = math.radians
    a = (math.sin(p(lat2 - lat1) / 2) ** 2
         + math.cos(p(lat1)) * math.cos(p(lat2)) * math.sin(p(lon2 - lon1) / 2) ** 2)
    return 2 * 6371 * math.asin(math.sqrt(a))


def pick_stations(table: dict[str, Any], lat: float, lon: float) -> tuple[str, str, float, float]:
    """(primary id, secondary id, primary km, secondary km)."""
    rows = []
    for sid, st in table.items():
        slat = st["lat"][0] + st["lat"][1] / 60
        slon = st["lon"][0] + st["lon"][1] / 60
        rows.append((_km(lat, lon, slat, slon), sid, st))
    rows.sort(key=lambda r: r[0])
    primary = next((r for r in rows if str(r[2].get("elems", "0"))[:1] == "1"), None)
    secondary = next((r for r in rows if r[2].get("type") == "A"), None)
    if primary is None or secondary is None:
        raise JmaParseError("the station table has no station with temperature or no type A station")
    return primary[1], secondary[1], primary[0], secondary[0]


def area_chain(area_json: dict[str, Any], class20: str) -> tuple[str, str]:
    """(class10, office) for a class20 code."""
    c15 = area_json["class20s"][class20]["parent"]
    c10 = area_json["class15s"][c15]["parent"]
    return c10, area_json["class10s"][c10]["parent"]


def device_title(area_json: dict[str, Any], class20: str) -> str:
    en = area_json["class20s"][class20].get("enName") or class20
    for suffix in (" City", " Ward", " Town", " Village"):
        en = en.removesuffix(suffix)
    return f"JMA {en}"


class JmaConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._area_json: dict[str, Any] = {}
        self._amedas: dict[str, Any] = {}
        self._office: str | None = None
        self._area: str | None = None
        self._stations: tuple[str, str, float, float] | None = None

    async def _load(self) -> None:
        api = JmaApi(async_get_clientsession(self.hass))
        self._area_json = await api.get_json(URL_AREA)
        self._amedas = await api.get_json(URL_AMEDAS_TABLE)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if not self._area_json:
            try:
                await self._load()
            except JmaError:
                return self.async_abort(reason="cannot_connect")
        if user_input is not None:
            self._office = user_input[CONF_OFFICE]
            return await self.async_step_area()
        try:
            options = [SelectOptionDict(value=code, label=f"{o['name']} ({code})")
                       for code, o in sorted(self._area_json["offices"].items())]
        except _SHAPE_ERRORS as err:
            _LOGGER.warning("The JMA area list is not in the expected shape: %r", err)
            options = []
        if not options:
            return self.async_abort(reason="cannot_connect")
        return self.async_show_form(step_id="user", data_schema=vol.Schema({
            vol.Required(CONF_OFFICE): SelectSelector(SelectSelectorConfig(
                options=options, mode=SelectSelectorMode.DROPDOWN)),
        }))

    async def async_step_area(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._area = user_input[CONF_AREA]
            await self.async_set_unique_id(self._area)
            self._abort_if_unique_id_configured()
            return await self.async_step_confirm()
        options = []
        try:
            for code, c20 in sorted(self._area_json["class20s"].items()):
                try:
                    if area_chain(self._area_json, code)[1] == self._office:
                        options.append(SelectOptionDict(value=code, label=f"{c20['name']} ({code})"))
                except (LookupError, TypeError, AttributeError):
                    continue
        except _SHAPE_ERRORS as err:
            _LOGGER.warning("The JMA area list is not in the expected shape: %r", err)
        if not options:
            return self.async_abort(reason="cannot_connect")
        return self.async_show_form(step_id="area", data_schema=vol.Schema({
            vol.Required(CONF_AREA): SelectSelector(SelectSelectorConfig(
                options=options, mode=SelectSelectorMode.DROPDOWN)),
        }))

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._area is not None
        try:
            if self._stations is None:
                self._stations = pick_stations(
                    self._amedas, self.hass.config.latitude, self.hass.config.longitude)
            primary, secondary, pkm, skm = self._stations
            area = self._area_json["class20s"][self._area]
            class10, office = area_chain(self._area_json, self._area)
            title = device_title(self._area_json, self._area)
            area_name = area["name"]
            placeholders = {
                "area": f"{area_name} ({self._area})",
                "primary": f"{self._amedas[primary]['kjName']} {primary} ({pkm:.1f} km)",
                "secondary": f"{self._amedas[secondary]['kjName']} {secondary} ({skm:.1f} km)",
            }
        except _SHAPE_ERRORS as err:
            _LOGGER.warning("The JMA area list or the station table is not in the expected shape: %r", err)
            return self.async_abort(reason="cannot_connect")
        if user_input is not None:
            return self.async_create_entry(
                title=title,
                data={
                    CONF_OFFICE: office,
                    CONF_AREA: self._area,
                    "area_name": area_name,
                    CONF_AREA_NAME_JA: area_name,  # the quake part matches addresses with it
                    "class10": class10,
                    CONF_STATION_PRIMARY: primary,
                    CONF_STATION_SECONDARY: secondary,
                },
            )
        return self.async_show_form(step_id="confirm", description_placeholders=placeholders)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return JmaOptionsFlow()


# ASCII digits only: `\d` also matches full-width and Arabic-Indic digits, which 404 in the station URL.
STATION_ID = re.compile(r"[0-9]{5}")


class JmaOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        data = {**self.config_entry.data, **self.config_entry.options}
        errors: dict[str, str] = {}
        if user_input is not None:
            cleaned = dict(user_input)
            for key in (CONF_STATION_PRIMARY, CONF_STATION_SECONDARY):
                # A typo used to 404 silently and quietly move every reading to the other station.
                # NFKC folds the full-width digits a Japanese input method types (４４１３２) to ASCII.
                cleaned[key] = unicodedata.normalize("NFKC", str(user_input.get(key, "")).strip())
                if not STATION_ID.fullmatch(cleaned[key]):
                    errors[key] = "invalid_station"
            if not errors:
                return self.async_create_entry(data=cleaned)
            data = {**data, **cleaned}
        # The five-digit check is made above and NOT in the schema: Home Assistant has to turn the
        # schema into form fields, and a vol.Match cannot be turned into one (the Configure dialog
        # answered HTTP 500 on 2026.9.4; tests that call the flow in Python never saw it).
        return self.async_show_form(step_id="init", errors=errors, data_schema=vol.Schema({
            vol.Required(CONF_STATION_PRIMARY, default=str(data[CONF_STATION_PRIMARY])): str,
            vol.Required(CONF_STATION_SECONDARY, default=str(data[CONF_STATION_SECONDARY])): str,
            vol.Required(CONF_DEBUG_SIMULATE, default=bool(data.get(CONF_DEBUG_SIMULATE, False))): bool,
        }))
