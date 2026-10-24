"""Config flow: forecast office -> warnings area (class20) -> confirm the auto-picked stations. Reconfigure picks the
stations again for an entry that exists.

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

import probatio as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
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
    CONF_VOLCANO_RADIUS_KM,
    DOMAIN,
    URL_AMEDAS_TABLE,
    URL_AREA,
    VOLCANO_RADIUS_MAX_KM,
)
from .errors import JmaError, JmaParseError
from .parse_volcano import radius_km

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


# The suffixes JMA's English names end in, each with how big a kind of place it says (a city or ward before a town
# before a village); a name with none of them ranks last.
_SUFFIX_RANK = ((" City", 0), (" Ward", 0), (" Town", 1), (" Village", 2))


def _english_name(row: Any, code: str) -> str:
    """The `enName` of a class20 row; the code when the row has none (or is not a row)."""
    name = row.get("enName") if isinstance(row, dict) else None
    return name if isinstance(name, str) and name else code


def _short_name(en: str) -> str:
    for suffix, _ in _SUFFIX_RANK:
        en = en.removesuffix(suffix)
    return en


def _rank(en: str) -> int:
    return next((rank for suffix, rank in _SUFFIX_RANK if en.endswith(suffix)), len(_SUFFIX_RANK))


def _region(area_json: dict[str, Any], class20: str) -> str | None:
    """The English name of the forecast office a class20 belongs to ("Tokyo", "Yamanashi"), for the brackets in a
    device title. JMA names an office after its prefecture; a trailing " Prefecture" and a bracketed note
    ("Kagoshima (Excluding Amami)") are cut so that the title stays short. None when the table cannot say."""
    try:
        name = area_json["offices"][area_chain(area_json, class20)[1]]["enName"]
    except (LookupError, TypeError, AttributeError):
        return None
    if not isinstance(name, str):
        return None
    return re.sub(r"\s*\(.*\)$", "", name.removesuffix(" Prefecture")).strip() or None


def device_title(area_json: dict[str, Any], class20: str) -> str:
    """"JMA <English name without City/Ward/Town/Village>". 102 of those names in JMA's table belong to more than one
    municipality (Chuo is a ward of Tokyo and a city in Yamanashi): two entries with one title get one device name and
    entity ids ending in _2. A shared name is therefore followed by its forecast office in brackets ("JMA Chuo
    (Tokyo)"). The one exception is the biggest kind of place when it is alone in its kind (a city before a town before
    a village): Chiyoda City keeps "JMA Chiyoda" and the Chiyoda Town in Gunma becomes "JMA Chiyoda (Gunma)", so the
    plain title that people already have does not change. Where two places of one office still share the short name
    (Fuchu City and Fuchu Town, both in Hiroshima) the whole English name is used."""
    places = area_json["class20s"]
    en = _english_name(places[class20], class20)
    short = _short_name(en)
    rivals = [code for code, row in places.items()
              if code != class20 and _short_name(_english_name(row, code)) == short]
    if all(_rank(en) < _rank(_english_name(places[code], code)) for code in rivals):
        return f"JMA {short}"  # no rivals at all, or the only city among towns
    region = _region(area_json, class20)
    same_office = any(_region(area_json, code) == region for code in rivals)
    return f"JMA {en if same_office else short} ({region or class20})"


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

    def _describe(self, area_code: str) -> tuple[dict[str, Any], str, dict[str, str]]:
        """(entry data, title, description placeholders) for a class20 code, from the tables fetched by `_load` and
        the stations nearest to Home Assistant's home. One place for the confirm step and Reconfigure, so what the
        form shows is what is stored. A table of the wrong shape raises one of `_SHAPE_ERRORS`."""
        if self._stations is None:
            self._stations = pick_stations(
                self._amedas, self.hass.config.latitude, self.hass.config.longitude)
        primary, secondary, pkm, skm = self._stations
        area_name = self._area_json["class20s"][area_code]["name"]
        class10, office = area_chain(self._area_json, area_code)
        data = {
            CONF_OFFICE: office,
            CONF_AREA: area_code,
            "area_name": area_name,
            CONF_AREA_NAME_JA: area_name,  # the quake part matches addresses with it
            "class10": class10,
            CONF_STATION_PRIMARY: primary,
            CONF_STATION_SECONDARY: secondary,
        }
        placeholders = {
            "area": f"{area_name} ({area_code})",
            "primary": f"{self._amedas[primary]['kjName']} {primary} ({pkm:.1f} km)",
            "secondary": f"{self._amedas[secondary]['kjName']} {secondary} ({skm:.1f} km)",
        }
        return data, device_title(self._area_json, area_code), placeholders

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._area is not None
        try:
            data, title, placeholders = self._describe(self._area)
        except _SHAPE_ERRORS as err:
            _LOGGER.warning("The JMA area list or the station table is not in the expected shape: %r", err)
            return self.async_abort(reason="cannot_connect")
        if user_input is not None:
            return self.async_create_entry(title=title, data=data)
        return self.async_show_form(step_id="confirm", description_placeholders=placeholders)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """HA quality scale: reconfiguration-flow. Picks the two AMeDAS stations again from the current tables and
        Home Assistant's home, and refreshes the area names the entry was made with (an entry made before 0.6 has no
        Japanese area name). The area stays: it is the entry's unique id, and so does the entry's title. A station
        typed into Configure is dropped so that the picked ones apply; the debug option stays."""
        entry = self._get_reconfigure_entry()
        area = entry.data[CONF_AREA]
        try:
            await self._load()  # at every call: a form left open for an hour must not apply an old table
        except JmaError:
            return self.async_abort(reason="cannot_connect")
        self._stations = None
        try:
            data, _title, placeholders = self._describe(area)
        except _SHAPE_ERRORS as err:
            _LOGGER.warning("The JMA area list or the station table is not in the expected shape: %r", err)
            return self.async_abort(reason="cannot_connect")
        if user_input is None:
            return self.async_show_form(step_id="reconfigure", description_placeholders=placeholders)
        await self.async_set_unique_id(area)
        self._abort_if_unique_id_mismatch()
        keep = {key: value for key, value in entry.options.items()
                if key not in (CONF_STATION_PRIMARY, CONF_STATION_SECONDARY)}
        # Not async_update_reload_and_abort: this entry has an update listener (__init__._reload) that reloads it when
        # its settings changed, and Home Assistant 2026.9 logs a usage report (and 2026.12 breaks) for an entry that
        # has both. An entry that is not set up has no listener and reads the new data when it next sets up.
        return self.async_update_and_abort(
            entry,
            data_updates={key: value for key, value in data.items() if key not in (CONF_OFFICE, CONF_AREA)},
            options=keep,
            # No title=: the title is the user's (they may have renamed the entry in Settings). A renamed municipality
            # or a new way of naming applies to entries made from now on.
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return JmaOptionsFlow()


# ASCII digits only: `\d` also matches full-width and Arabic-Indic digits, which 404 in the station URL.
STATION_ID = re.compile(r"[0-9]{5}")


class JmaOptionsFlow(OptionsFlow):
    async def _station_table(self) -> dict[str, Any] | None:
        """The agency's AMeDAS station table, or None when it cannot be had: the ids are then accepted as typed, for the
        user is not to be held up by JMA being down (the five-digit check has been made already)."""
        try:
            table = await JmaApi(async_get_clientsession(self.hass)).get_json(URL_AMEDAS_TABLE)
        except JmaError as err:
            _LOGGER.debug("amedastable unavailable, station ids accepted as typed: %s", err)
            return None
        if not isinstance(table, dict):
            _LOGGER.debug("amedastable is not an object, station ids accepted as typed")
            return None
        return table

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
            if not errors and (table := await self._station_table()) is not None:
                # A well-formed id that is not in the agency's table is a typo: it 404s and quietly moves every
                # reading to the other station until the feed-problem sensor notices.
                for key in (CONF_STATION_PRIMARY, CONF_STATION_SECONDARY):
                    if cleaned[key] not in table:
                        errors[key] = "unknown_station"
            if CONF_VOLCANO_RADIUS_KM in cleaned:
                # The number box answers a float (80.0); a field left empty is not sent and the default applies.
                cleaned[CONF_VOLCANO_RADIUS_KM] = int(radius_km(cleaned[CONF_VOLCANO_RADIUS_KM]))
            if not errors:
                return self.async_create_entry(data=cleaned)
            data = {**data, **cleaned}
        # The five-digit check is made above and NOT in the schema: Home Assistant has to turn the
        # schema into form fields, and a vol.Match cannot be turned into one (the Configure dialog
        # answered HTTP 500 on 2026.9.4; tests that call the flow in Python never saw it).
        return self.async_show_form(step_id="init", errors=errors, data_schema=vol.Schema({
            vol.Required(CONF_STATION_PRIMARY, default=str(data[CONF_STATION_PRIMARY])): str,
            vol.Required(CONF_STATION_SECONDARY, default=str(data[CONF_STATION_SECONDARY])): str,
            # Optional with a suggested value, not a default: an options form that does not send it (a client that
            # predates it) leaves the saved options as they were, and the default radius applies until one is set.
            vol.Optional(CONF_VOLCANO_RADIUS_KM, description={
                "suggested_value": int(radius_km(data.get(CONF_VOLCANO_RADIUS_KM)))}):
                NumberSelector(NumberSelectorConfig(
                    min=0, max=VOLCANO_RADIUS_MAX_KM, step=1, unit_of_measurement="km",
                    mode=NumberSelectorMode.BOX)),
            vol.Required(CONF_DEBUG_SIMULATE, default=bool(data.get(CONF_DEBUG_SIMULATE, False))): bool,
        }))
